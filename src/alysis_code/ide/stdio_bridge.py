from __future__ import annotations

import hashlib
import hmac
import inspect
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import sys
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from contextlib import nullcontext, redirect_stdout, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlparse, urlsplit, urlunsplit

from .. import __version__, feedback_delivery
from ..agent.action_approval import decide_action_approval
from ..agent.browser_start import (
    BrowserStartHandoff,
    valid_browser_start_url,
    valid_computer_selection,
)
from ..agent.computer_use_files import (
    COMPUTER_USE_FILE_PROMPT,
    COMPUTER_USE_FILE_TOOL_NAMES,
    validate_file_result,
)
from ..agent.computer_use_tools import (
    COMPUTER_USE_RESPONSE_MODES,
    COMPUTER_USE_SYSTEM_PROMPT,
    COMPUTER_USE_TOOL_NAMES,
    COMPUTER_USE_VERIFICATION_PROMPT,
    COMPUTER_USE_VERIFICATION_TOOL_NAMES,
)
from ..agent.computer_use_upload_objectives import UploadObjectiveBinding, capture_upload_objective
from ..agent.computer_use_uploads import (
    COMPUTER_USE_UPLOAD_PROMPT,
    COMPUTER_USE_UPLOAD_TOOL_NAMES,
    upload_result_projection,
)
from ..agent.prompt_context import (
    MAX_IMAGE_BYTES,
    resolve_session_active_workdir_path,
    resolve_session_active_workdir_relpath,
    set_session_active_workdir,
)
from ..agent.session import create_session
from ..agent.steering import MAX_PENDING_STEER_MESSAGES, steer_inbox_for
from ..approval_scope import (
    SCOPE_EXACT_COMMAND_HASH,
    SCOPE_EXACT_FILE_SET,
    SCOPE_EXACT_VERIFY_COMMAND_SET,
    approval_request_is_destructive,
    exact_command_scope,
    exact_file_set_scope,
    exact_verify_command_set_scope,
)
from ..branding import env_get
from ..cancellation import CooperativeCancellationError, EventCancellationToken
from ..code_review import (
    ChatReviewerClient,
    CodeReviewEngine,
    InvalidReviewRequest,
    ReviewRequest,
)
from ..command_text import command_text
from ..config import (
    ConfigError,
    _apply_legacy_temperature_override,
    clone_cfg,
    load_config,
    save_config,
)
from ..feedback_report import FeedbackReportError
from ..host_actions import (
    HOST_ACTION_MAX_ARGUMENT_BYTES,
    HOST_ACTION_MAX_RESULT_BYTES,
    HOST_ACTION_PROTOCOL_VERSION,
    HOST_ACTION_SET,
    HOST_ACTIONS,
    HostActionError,
    json_size_bytes,
    normalize_host_action_arguments,
    normalize_host_action_result,
    normalize_host_error,
)
from ..host_images import (
    HOST_SCREEN_REDACTION_MARKER,
    HostImageBinding,
    HostImageValidationError,
    HostScreenRedactor,
    ValidatedHostImage,
    is_configured_host_image_candidate,
    parse_host_image_binding_descriptor,
    read_validated_host_image,
    use_ide_host_image_binding,
    validate_ide_host_image,
)
from ..llm.base import RequiredToolCallingUnsupportedError
from ..llm.metadata import credential_scope_fingerprint
from ..llm.protocols import OPENAI_COMPAT_PROTOCOL
from ..mcp.errors import McpError
from ..mcp.github_discovery import ACTIONS as GITHUB_DISCOVERY_ACTIONS
from ..mcp.github_discovery import GitHubDiscoveryState
from ..mcp.github_host import (
    GITHUB_MCP_ACTION,
    assert_host_mcp_current,
    bind_host_mcp_catalog,
    host_mcp_session_payload,
    normalize_host_mcp_catalog,
)
from ..mcp.google_host import (
    GOOGLE_MCP_ACTION,
    assert_google_current,
    bind_google_catalogs,
    google_session_payload,
    normalize_google_catalogs,
)
from ..mcp.manager import McpManager
from ..permission_policy import (
    PermissionEvaluation,
    PermissionPolicyError,
    PermissionPolicyStore,
    PermissionRequest,
    PolicyEffect,
    default_permission_policy_path,
)
from ..personas import (
    BUILTIN_PERSONAS,
    DEFAULT_PERSONA,
    PERSONA_NAMES,
    all_personas,
    get_persona,
    is_persona_name,
    load_custom_personas,
    normalize_persona,
    persona_modes_enabled,
)
from ..profiles import (
    ProfileSpec,
    add_profile,
    get_active_profile,
    resolve_effective_base_url,
    set_active_profile,
    validate_base_url,
)
from ..provider_auth.base import ProviderAuthError
from ..request_estimation import estimate_request_token_breakdown, estimate_request_tokens
from ..run_outcome import task_outcome_fields
from ..runtime_kind import RuntimeKind
from ..sandbox_settings import apply_sandbox_mode_to_config, resolve_shell_sandbox_settings
from ..session_store import make_session_id, resolve_sessions_dir
from ..surface.console import make_console
from ..surface.events import InfoEmitted
from ..surface.types import ApprovalDecision, ApprovalRequest, StandingApproval
from ..swarm_orchestrator import run_swarm
from ..task_budget import TaskBudgetAuthority, TaskBudgetRefused, task_budget_setup
from ..task_budget import iso as budget_iso
from ..task_budget import need as budget_need
from ..task_budget import parse_binding as parse_task_budget_binding
from ..task_budget import stamp as budget_stamp
from ..tools.fs import _git_check_ignored, classify_sensitive_path
from ..tools.history import HistorySearchError, history_search
from ..tools.registry import built_in_subagent_tool_names
from ..usage_tracker import aggregate_usage_from_session_logs
from ..workspace_binding import WorkspaceAction, WorkspaceBindingError
from ..workspace_binding_ui import resolve_startup_workspace_binding
from .activity_events import ActivityEvent
from .approval_binding import (
    ApprovalBinding,
    approval_policy_fingerprint,
    build_approval_binding,
    normalize_connector_approval_context,
    runtime_instance_fingerprint,
)
from .artifacts import ArtifactRoot, ArtifactStore
from .cdp_websocket_transport import WebSocketCdpTransportFactory
from .change_ledger import (
    ChangeLedger,
    ChangeLedgerError,
    Checkpoint,
    StaleWorkspaceError,
    checkpoint_storage_exists,
)
from .context_blocks import ContextValidationError, sanitize_context_blocks
from .event_stream import EventContext, EventSequencer, ProtocolEventSurface, ProtocolPayloadEvent
from .forge_protocol import (
    ForgePlanRecord,
    create_ide_forge_plan,
    diff_get_result,
    diff_list_result,
    ensure_expected_plan_revision,
    forge_artifact_root_name,
    forge_assets_add_result,
    forge_assets_cancel_pending_result,
    forge_assets_check_plan_result,
    forge_assets_delete_result,
    forge_assets_edit_result,
    forge_assets_list_result,
    forge_assets_prune_legacy_result,
    forge_assets_refresh_result,
    forge_assets_show_result,
    forge_attach_result,
    forge_execute_preview_result,
    forge_execute_review_job_result,
    forge_plan_edit_render,
    forge_plan_regenerate_commit,
    forge_plan_regenerate_compute,
    forge_plan_regenerate_render,
    forge_plan_result,
    forge_plan_set_assistant_apply,
    forge_plan_set_goal_apply,
    forge_plan_state_result,
    forge_plan_update_task_apply,
    forge_plan_validate_result,
    forge_review_result,
    forge_show_result,
    forge_status_result,
    list_persisted_forge_plans,
    load_recorded_plan,
    open_persisted_forge_plan,
    validate_forge_run_artifact_root,
)
from .forge_request_ledger import (
    DurableForgeRequestLedger,
    ForgeDispatchLease,
    ForgeRequestIdempotencyConflict,
    ForgeRequestLeaseLost,
    ForgeRequestLedgerError,
    ForgeRequestRecord,
    ForgeRequestState,
    ForgeWorkerLease,
)
from .health import SUPPORTED_MODES, capabilities_payload, health_payload
from .managed_browser import (
    BrowserArtifact,
    BrowserError,
    BrowserSessionStatus,
    ManagedBrowserConfig,
    ManagedBrowserService,
)
from .management_protocol import (
    MANAGEMENT_METHODS,
    handle_management_method,
    prepare_mcp_oauth_login_request,
    retained_session_usage,
)
from .mcp_oauth_coordinator import (
    McpOAuthCoordinatorConfig,
    McpOAuthCoordinatorError,
    McpOAuthIdeCoordinator,
)
from .mcp_oauth_lifecycle import OAuthFlowStateError, OAuthFlowStatus, OAuthValidationError
from .parent_watchdog import start_parent_watchdog
from .prompt_queue import (
    DEFAULT_LEASE_SECONDS,
    DurablePromptQueue,
    PromptLease,
    PromptLeaseLost,
    PromptQueueError,
    PromptQueueItem,
    PromptState,
)
from .protocol import (
    MAX_REQUEST_BYTES,
    ProtocolError,
    ProtocolRequest,
    RequestId,
    dumps_message,
    error_message,
    parse_request_line,
    redact_secrets,
    response_message,
)
from .resumable_swarm import (
    DurableResumableSwarmCoordinator,
    ResumableSwarmError,
    SwarmFreshPermissionGrantRequired,
    SwarmJobNotFound,
    SwarmJobState,
    SwarmLeaseLost,
    SwarmPermissionScopeChanged,
    SwarmUsage,
    SwarmWorkerLease,
)
from .session_search import (
    SessionSearchError,
    SessionSearchLimits,
    past_session_context_block,
    search_workspace_sessions,
)
from .skill_invocation import skill_context_message, validate_skill_invocation
from .structured_state import (
    DurableStructuredState,
    QuestionIdempotencyConflict,
    QuestionNotFound,
    QuestionStateError,
    StructuredStateError,
    TaskRevisionConflict,
)
from .swarm_protocol import (
    BridgeSwarmTraceSink,
    forge_swarm_apply_result,
    forge_swarm_discard_result,
    forge_swarm_reconcile_result,
    forge_swarm_result_payload,
    forge_swarm_review_result,
    harvest_ready_review_diffs,
)
from .task_budget_channel import TaskBudgetChannel

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SESSION_ID_MAX_LENGTH = 128
IDE_SESSION_DIR_ROOT_ENV = "ALYSIS_IDE_SESSION_DIR_ROOT"
EVENT_REPLAY_MAX = 1_000
EVENT_REPLAY_RESPONSE_MAX = 500
DEFAULT_CLOSED_SESSION_HISTORY_MAX = 100
DEFAULT_JOB_HISTORY_MAX = 1_000
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 2.0
# Extra time the parent watchdog allows the normal shutdown to finish before os._exit.
PARENT_EXIT_GRACE_SECONDS = 5.0
TRACE_EVENT_RESPONSE_MAX = 500
TRACE_ARTIFACT_DEFAULT_MAX_BYTES = 32 * 1024
TRACE_ARTIFACT_MAX_BYTES = 256 * 1024
TERMINAL_OUTPUT_RESPONSE_MAX_LINES = 500
TERMINAL_OUTPUT_DEFAULT_MAX_LINES = 200
TERMINAL_OUTPUT_DEFAULT_MAX_BYTES = 32 * 1024
TERMINAL_OUTPUT_MAX_BYTES = 256 * 1024
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300.0
DEFAULT_HOST_ACTION_TIMEOUT_SECONDS = 30.0
HOST_ACTION_RESOLUTION_RETENTION_SECONDS = 300.0
HOST_ACTION_RESOLUTION_MAX = 512
MAX_COMPUTER_USE_APPROVAL_SECONDS = 120.0
DEFAULT_COMPUTER_USE_TIMEOUT_SECONDS = 45.0
APPROVAL_RESOLUTION_RETENTION_SECONDS = 300.0
APPROVAL_RESOLUTION_MAX = 512
FINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})
ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "cancellation_requested"})
FILE_APPROVAL_KINDS = frozenset(
    {
        "fs_write",
        "fs_edit",
        "fs_move",
        "fs_copy",
        "fs_delete",
        "fs_mkdir",
        "git_apply_patch",
    }
)
SHELL_APPROVAL_KINDS = frozenset({"shell_run", "shell_background"})
MAX_IDE_IMAGE_BYTES = MAX_IMAGE_BYTES
# Reviewed feedback reports kept for feedback.send; the oldest is dropped first.
MAX_PREPARED_FEEDBACK = 16
CHAT_SEND_ALLOWED_FIELDS = frozenset(
    {
        "session_id",
        "message",
        "instruction",
        "images",
        "image_paths",
        "context_blocks",
        "skill",
        "host_image",
        "idempotency_key",
    }
)
RUN_START_SESSION_FIELDS = frozenset(
    {
        "workspace",
        "session_dir",
        "mode",
        "session_id",
        "temperature",
        "stream",
        "verify_cmd",
        "verify_commands",
        "subagents_enabled",
        "no_log",
        "yes",
        "max_steps",
        "model",
        "base_url",
        "computer_use_destination",
        "sensitive_media",
        "computer_use_host",
        "active_workdir",
        "active_workdir_relpath",
        "workspace_trusted",
        "host_capabilities",
        "host_mcp",
        "google_mcp",
    }
)
RUN_START_TURN_FIELDS = CHAT_SEND_ALLOWED_FIELDS - {"session_id"}
RUN_START_ALLOWED_FIELDS = RUN_START_SESSION_FIELDS | RUN_START_TURN_FIELDS
SUPPORTED_IDE_IMAGE_MIME_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
    }
)
TRACE_LEVELS = frozenset({"off", "compact", "full"})
COMPUTER_USE_DESTINATION_VERSION = 1
_SHA256_WITH_PREFIX_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
# A sensitive-media session is a single-purpose, answer-only capability.  Keep
# this allowlist deliberately small: a request which could change the model,
# route, tool surface, prompt state, workdir, or durable state cannot race the
# one-shot host-image turn.  ``chat.send`` is the only mutating turn operation
# that remains available, and it independently rechecks the route binding.
SENSITIVE_MEDIA_ALLOWED_SESSION_METHODS = frozenset(
    {
        "chat.send",
        "job.status",
        "session.status",
        "session.usage",
        "session.modelInfo",
        "session.cancel",
        "session.getEvents",
        "computer.respond",
        "task_budget.respond",
        "approval.respond",
    }
)
# Provider failures sometimes echo an encoded request body without a data-URL
# prefix.  For the sensitive worker error boundary it is safer to discard such
# a blob than to risk retaining screen pixels in a job/event transcript.
_SENSITIVE_MEDIA_BASE64_BLOB_PATTERN = re.compile(
    r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{128,}={0,2}(?![A-Za-z0-9+/=_-])"
)


class BridgeCancellationError(CooperativeCancellationError):
    pass


class BridgeSessionCleanupError(RuntimeError):
    """A retryable owned-resource cleanup failure with a secret-safe message."""


class BridgeCancellationToken(EventCancellationToken):
    error_class = BridgeCancellationError


@dataclass(frozen=True, slots=True)
class ComputerUseDestinationBinding:
    """Nonsecret, immutable proof of the client route approved for host pixels."""

    model: str
    profile: str
    auth_source: str
    account_id: str
    endpoint_digest: str
    route_fingerprint: str

    def to_protocol(self) -> dict[str, Any]:
        return {
            "version": COMPUTER_USE_DESTINATION_VERSION,
            "model": self.model,
            "profile": self.profile,
            "auth_source": self.auth_source,
            "account_id": self.account_id,
            "endpoint_digest": self.endpoint_digest,
            "route_fingerprint": self.route_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ComputerUseHostBinding:
    """Main-process proof that a trusted, supervised native host is attached."""

    operation_id: str
    broker_fingerprint: str
    authority_epoch: int
    scope_digest: str
    response_mode: str = "status_only"
    observation_version: int | None = None
    verification_available: bool = False
    files_available: bool = False
    uploads_available: bool = False
    task_budget: dict[str, Any] | None = None
    upload_objective: UploadObjectiveBinding | None = None

    def __post_init__(self) -> None:
        objective = capture_upload_objective(self.upload_objective)
        if objective is not None and not (self.uploads_available and self.verification_available):
            raise ValueError("Upload objective evidence requires upload and verification tools")
        object.__setattr__(self, "upload_objective", objective)

    def to_protocol(self) -> dict[str, Any]:
        return {
            "version": 1,
            "operation_id": self.operation_id,
            "broker_fingerprint": self.broker_fingerprint,
            "authority_epoch": self.authority_epoch,
            "scope_digest": self.scope_digest,
            "tools": list(
                COMPUTER_USE_TOOL_NAMES
                + (COMPUTER_USE_VERIFICATION_TOOL_NAMES if self.verification_available else ())
                + (COMPUTER_USE_FILE_TOOL_NAMES if self.files_available else ())
                + (COMPUTER_USE_UPLOAD_TOOL_NAMES if self.uploads_available else ())
            ),
            "response_mode": self.response_mode,
            **({"observation_version": 1} if self.observation_version == 1 else {}),
            **(
                {"task_budget": _json_clone(self.task_budget)}
                if self.task_budget is not None
                else {}
            ),
            **(
                {"upload_objective": self.upload_objective.to_protocol()}
                if self.upload_objective is not None
                else {}
            ),
        }


@dataclass
class PendingComputerUseRequest:
    request_id: str
    response_nonce: str
    session_id: str
    operation_id: str
    job_id: str
    tool_call_id: str
    tool_name: str
    arguments_fingerprint: str
    expires_at: str
    deadline: float
    approval_preflight_expected: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    resolved: bool = False


@dataclass(frozen=True, slots=True)
class ApprovalScopeRecord:
    kind: str
    scope: dict[str, Any]
    key: str
    grant_id: str = field(default_factory=lambda: f"sg_{uuid.uuid4().hex}")


@dataclass(frozen=True, slots=True)
class ResolvedApprovalRecord:
    response: dict[str, Any]
    resolved_at: float
    duplicate_error: bool = False


@dataclass
class PendingApprovalRecord:
    approval_id: str
    session_id: str
    kind: str
    reason: str
    preview: str
    files: list[str]
    command: str | None
    metadata: dict[str, Any]
    scope: dict[str, Any] | None
    scope_key: str | None
    allow_for_session_supported: bool
    allow_for_session_warning: str | None
    expires_at: str | None
    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    decision: ApprovalDecision | None = None
    response: dict[str, Any] | None = None
    result_status: str | None = None
    resolved: bool = False
    binding: ApprovalBinding | None = None
    # Private host boundary, never sourced from ApprovalRequest.metadata.
    host_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResolvedHostActionRecord:
    status: str
    resolved_at: float


@dataclass
class PendingHostActionRecord:
    host_action_id: str
    session_id: str
    job_id: str
    action: str
    arguments: dict[str, Any]
    workspace_fence: str
    capability_fingerprint: str
    expires_at: str
    deadline: float
    write_effect: bool = False
    phase: str = "execute"
    dispatched: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: HostActionError | None = None
    status: str = "pending"
    resolved: bool = False


@dataclass
class BridgeJob:
    job_id: str
    session_id: str
    created_at: str
    kind: str = "session_turn"
    status: str = "queued"
    started_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None
    exit_code: int | None = None
    error: str | None = None
    error_code: str | None = None
    thread: threading.Thread | None = None
    plan_id: str | None = None
    result: dict[str, Any] | None = None
    cancellable: bool = True
    cancellation_reason: str | None = None
    cancellation_requested_at: str | None = None
    event_count: int = 0
    dropped_event_count: int = 0
    prompt_id: str | None = None
    prompt_sequence: int | None = None
    prompt_lease_token: str | None = field(default=None, repr=False)
    prompt_lease_expires_at: float | None = field(default=None, repr=False)
    prompt_reconciliation_started: bool = field(default=False, repr=False)
    cancellation_event: threading.Event = field(default_factory=threading.Event)
    durable_swarm_lease: SwarmWorkerLease | None = field(default=None, repr=False)
    durable_forge_lease: ForgeWorkerLease | None = field(default=None, repr=False)
    # This descriptor is carried only by this worker. It is never copied to a
    # process environment, session state, event, or subsequent job.
    browser_start_handoff: dict[str, str] | None = field(default=None, repr=False)
    host_image_binding: HostImageBinding | None = None
    prepared_host_image: tuple[ValidatedHostImage, bytes] | None = field(default=None, repr=False)


@dataclass
class BridgeSession:
    session_id: str
    root: Path
    mode: str
    surface: ProtocolEventSurface
    agent_session: Any
    artifact_store: ArtifactStore
    change_ledger: ChangeLedger | None = None
    checkpoint_guard: _CheckpointToolDispatchGuard | None = None
    checkpoint_setup: tuple[threading.Event, dict[str, Any]] | None = field(
        default=None, repr=False
    )
    structured_state: DurableStructuredState | None = None
    resumable_swarm: DurableResumableSwarmCoordinator | None = None
    managed_browser: ManagedBrowserService | None = None
    active_job: BridgeJob | None = None
    last_job: BridgeJob | None = None
    forked_from_session_id: str | None = None
    # How the session's current persona was chosen ("config" until the IDE
    # sets one via session.persona.set, then "user"), mirroring the source
    # vocabulary of the persona_changed event.
    persona_source: str = "config"
    closed: bool = False
    close_when_idle: bool = False
    close_worker: threading.Thread | None = field(default=None, repr=False)
    sensitive_media: bool = False
    computer_use_destination: ComputerUseDestinationBinding | None = None
    browser_start_host: dict[str, Any] | None = None
    pending_browser_start: dict[str, Any] | None = field(default=None, repr=False)
    browser_start_consumed: bool = False
    computer_use_host: ComputerUseHostBinding | None = None
    task_budget_channel: TaskBudgetChannel | None = None
    task_budget_authority: TaskBudgetAuthority | None = None
    task_budget_consumed: bool = False
    host_image_descriptor_consumed: bool = False
    pending_images: list[str] = field(default_factory=list)
    # Steering messages earlier turns ended without reading (chat.steer.take hands them out).
    unread_steers: list[str] = field(default_factory=list)
    pending_approvals: dict[str, PendingApprovalRecord] = field(default_factory=dict)
    approved_approval_scopes: list[ApprovalScopeRecord] = field(default_factory=list)
    resolved_approvals: dict[str, ResolvedApprovalRecord] = field(default_factory=dict)
    approval_lock: threading.RLock = field(default_factory=threading.RLock)
    workspace_trusted: bool = False
    host_actions: frozenset[str] = field(default_factory=frozenset)
    host_approval_protocol: int = 0
    host_mcp: dict[str, Any] | None = None
    github_discovery: GitHubDiscoveryState = field(default_factory=GitHubDiscoveryState)
    google_mcp: list[dict[str, Any]] = field(default_factory=list)
    workspace_fence: str = ""
    host_capability_fingerprint: str = ""
    pending_host_actions: dict[str, PendingHostActionRecord] = field(default_factory=dict)
    resolved_host_actions: dict[str, ResolvedHostActionRecord] = field(default_factory=dict)
    host_action_lock: threading.RLock = field(default_factory=threading.RLock)
    close_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    pending_computer_use: dict[str, PendingComputerUseRequest] = field(default_factory=dict)
    resolved_computer_use_ids: set[str] = field(default_factory=set)
    computer_use_lock: threading.RLock = field(default_factory=threading.RLock)


# Built-in tools that cannot change the workspace: the registry's audited read-only set (the
# tools a read-only subagent may use), plus the original read names. Shell, MCP, delegation and
# unknown tools can change files indirectly and still wait for a restore baseline.
_CHECKPOINT_FREE_READ_TOOLS = frozenset(built_in_subagent_tool_names(exposure="readonly")) | (
    frozenset(
        {"fs_read", "fs_read_lines", "fs_list", "git_status", "git_diff", "session_artifact_read"}
    )
)


class _CheckpointToolDispatchGuard:
    """Prepare rollback storage before tools that can change the workspace."""

    def __init__(self, session_ref: dict[str, BridgeSession]) -> None:
        self._session_ref = session_ref
        self._prepared_job_id: str | None = None
        self._ready = False
        self._lock = threading.Lock()

    def check_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        resolve_rel_path: Callable[..., str] | None = None,
    ) -> None:
        session = self._session_ref.get("session")
        if session is None or not _checkpointing_supported(session):
            return
        job = session.active_job
        if job is None:
            return
        _check_job_cancelled(job)
        # Only audited built-ins bypass setup. Unknown tools, shell, MCP and
        # delegation can edit indirectly and must still share a baseline.
        if tool_name in _CHECKPOINT_FREE_READ_TOOLS:
            return
        with self._lock:
            _check_job_cancelled(job)
            # Not ready is checked again on the next call: the baseline worker keeps running
            # and may have finished since.
            if self._prepared_job_id != job.job_id or not self._ready:
                self._ready = _prepare_change_ledger_with_deadline(session)
                self._prepared_job_id = job.job_id
            _check_job_cancelled(job)
            if not self._ready:
                raise ChangeLedgerError(
                    "checkpoint_unavailable: This action was not run because restore "
                    "snapshots are not ready. File reading and Git review remain available. "
                    "Wait for setup to finish, then send your request again."
                )


class _TurnTouchedPathSet(set[str]):
    """Preserve cumulative session paths while recording repeated per-turn touches."""

    def __init__(self, values: Iterable[str] = ()) -> None:
        super().__init__(str(value) for value in values)
        self.recorded: set[str] = set()

    def add(self, element: str) -> None:
        self.recorded.add(str(element))
        super().add(str(element))

    def update(self, *others: Iterable[str]) -> None:
        for values in others:
            for value in values:
                self.add(str(value))

    def __ior__(self, other: Iterable[str]) -> _TurnTouchedPathSet:
        self.update(other)
        return self


class _PlanInspectionSession:
    def close(self) -> None:
        return


def _default_managed_browser_service(*, workspace_root: Path) -> ManagedBrowserService:
    return ManagedBrowserService(
        config=ManagedBrowserConfig(workspace_roots=(workspace_root,)),
        transport_factory=WebSocketCdpTransportFactory(),
    )


class StdioBridge:
    def __init__(
        self,
        *,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        create_session_fn: Callable[..., Any] = create_session,
        forge_execute_agent_runner: Callable[..., int] | None = None,
        forge_swarm_runner: Callable[..., int] | None = None,
        code_review_runner: Callable[[Path, Any, ReviewRequest], dict[str, Any]] | None = None,
        approval_timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
        host_action_timeout_seconds: float = DEFAULT_HOST_ACTION_TIMEOUT_SECONDS,
        event_replay_max: int = EVENT_REPLAY_MAX,
        closed_session_history_max: int = DEFAULT_CLOSED_SESSION_HISTORY_MAX,
        job_history_max: int = DEFAULT_JOB_HISTORY_MAX,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        prompt_queue: DurablePromptQueue | None = None,
        prompt_queue_owner_id: str | None = None,
        forge_request_ledger: DurableForgeRequestLedger | None = None,
        permission_policy_store: PermissionPolicyStore | None = None,
        structured_state_factory: Callable[..., DurableStructuredState] = DurableStructuredState,
        resumable_swarm_factory: Callable[..., DurableResumableSwarmCoordinator] = (
            DurableResumableSwarmCoordinator
        ),
        managed_browser_factory: Callable[..., ManagedBrowserService] = (
            _default_managed_browser_service
        ),
        mcp_oauth_coordinator: McpOAuthIdeCoordinator | None = None,
        mcp_oauth_coordinator_factory: Callable[[], McpOAuthIdeCoordinator] | None = None,
    ) -> None:
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self._create_session = create_session_fn
        self._forge_execute_agent_runner = forge_execute_agent_runner
        self._forge_swarm_runner = forge_swarm_runner
        self._code_review_runner = code_review_runner
        self._sessions: dict[str, BridgeSession] = {}
        self._jobs: dict[str, BridgeJob] = {}
        self._forge_plans: dict[str, ForgePlanRecord] = {}
        self._event_buffers: dict[str, deque[dict[str, Any]]] = {}
        self._event_dropped_counts: dict[str, int] = {}
        # session.trace.clear moves this per-session floor instead of touching
        # _event_buffers, so session.getEvents reconnect replay stays intact.
        self._trace_view_floors: dict[str, int] = {}
        self._event_replay_max = max(1, int(event_replay_max))
        self._closed_session_history_max = max(0, int(closed_session_history_max))
        self._job_history_max = max(1, int(job_history_max))
        self._shutdown_timeout_seconds = max(0.0, float(shutdown_timeout_seconds))
        self._approval_timeout_seconds = max(0.01, float(approval_timeout_seconds))
        self._host_action_timeout_seconds = max(0.01, float(host_action_timeout_seconds))
        self._prompt_queue = prompt_queue or DurablePromptQueue()
        self._prompt_queue_owner_id = (
            str(prompt_queue_owner_id or "").strip() or f"bridge-{uuid.uuid4().hex}"
        )
        # Lets the next engine see at once that this one is gone (stopped by its
        # host, or crashed), so a prompt it was running need not wait out its lease.
        hold_owner = getattr(self._prompt_queue, "hold_owner", None)
        if callable(hold_owner):
            with suppress(Exception):
                hold_owner(self._prompt_queue_owner_id)
        self._forge_request_ledger = forge_request_ledger or DurableForgeRequestLedger()
        self._permission_policy_store = permission_policy_store or PermissionPolicyStore(
            default_permission_policy_path()
        )
        self._structured_state_factory = structured_state_factory
        self._resumable_swarm_factory = resumable_swarm_factory
        self._managed_browser_factory = managed_browser_factory
        self._mcp_oauth_coordinator = mcp_oauth_coordinator
        self._mcp_oauth_coordinator_factory = mcp_oauth_coordinator_factory or (
            lambda: McpOAuthIdeCoordinator(
                config=McpOAuthCoordinatorConfig(
                    shutdown_wait_seconds=self._shutdown_timeout_seconds
                )
            )
        )
        self._runtime_fingerprint = runtime_instance_fingerprint(
            uuid.uuid4().hex,
            version=__version__,
        )
        # feedback.prepare keeps the exact reviewed bytes here until feedback.send.
        self._prepared_feedback: OrderedDict[str, feedback_delivery.PreparedFeedback] = (
            OrderedDict()
        )
        self._state_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._closed = False

    def _runtime_health_payload(self) -> dict[str, Any]:
        payload = health_payload()
        payload["runtime_instance_fingerprint"] = self._runtime_fingerprint
        return payload

    def _runtime_capabilities_payload(self) -> dict[str, Any]:
        payload = capabilities_payload()
        payload["runtime_instance_fingerprint"] = self._runtime_fingerprint
        return payload

    def run(self) -> int:
        try:
            while True:
                line = self.stdin.readline(MAX_REQUEST_BYTES + 1)
                if not line:
                    return 0
                if len(line) > MAX_REQUEST_BYTES and not line.endswith("\n"):
                    self._discard_oversized_request_line()
                    self._write(
                        error_message(
                            None,
                            "request_too_large",
                            "Request exceeds the maximum supported size.",
                        )
                    )
                    continue
                self.process_line(line)
                if self._closed:
                    return 0
        finally:
            self.close()

    def _discard_oversized_request_line(self) -> None:
        """Drain one rejected JSONL record so the next request stays aligned."""
        while True:
            remainder = self.stdin.readline(MAX_REQUEST_BYTES + 1)
            if not remainder or remainder.endswith("\n"):
                return

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            sessions = list(self._sessions.values())
            active: list[tuple[BridgeSession, BridgeJob]] = []
            for session in sessions:
                _reconcile_session_job_state(session)
                job = session.active_job
                if _job_is_active(job) and job is not None:
                    _request_job_cancellation(job, "bridge_shutdown_interrupted")
                    # A shutdown has no reason to retain one-shot host-media
                    # authority while it waits for a worker to observe cancel.
                    # A worker that has already captured it still checks cancel
                    # before any provider call; a worker which has not started
                    # cannot acquire it after this point.
                    job.host_image_binding = None
                    job.prepared_host_image = None
                    active.append((session, job))

        # Approval cancellation takes the per-session approval lock.  Keep it
        # outside _state_lock so an approval emitter can never deadlock shutdown.
        for session, job in active:
            self._cancel_pending_approvals(session, "bridge_shutdown_interrupted")
            self._cancel_pending_host_actions(session, "bridge_shutdown_interrupted")
            self._cancel_pending_computer_use(session, "bridge_shutdown_interrupted")
            session.surface.emit_warning(f"shutdown_active_job_interrupted {job.job_id}")

        deadline = time.monotonic() + self._shutdown_timeout_seconds
        current = threading.current_thread()
        for _session, job in active:
            thread = job.thread
            if thread is None or thread is current or not thread.is_alive():
                continue
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

        for session, job in active:
            lease = job.durable_swarm_lease
            coordinator = session.resumable_swarm
            if job.kind == "forge_swarm" and lease is not None and coordinator is not None:
                with suppress(ResumableSwarmError):
                    coordinator.interrupt(
                        lease,
                        error_code="bridge_shutdown_interrupted",
                        error_summary="The IDE bridge stopped before the swarm completed.",
                    )

        with self._state_lock:
            for session, job in active:
                if _job_is_active(job):
                    _mark_job_completed(
                        job,
                        status="failed",
                        exit_code=1,
                        error="bridge_shutdown_interrupted",
                    )
                _reconcile_session_job_state(session)
            self._prune_terminal_jobs_locked()

        cleanup_errors: list[BridgeSessionCleanupError] = []
        for session in sessions:
            for attempt in range(2):
                try:
                    self._close_session(session)
                    break
                except BridgeSessionCleanupError as exc:
                    if attempt == 0 and time.monotonic() < deadline:
                        continue
                    cleanup_errors.append(exc)
                    break
        coordinator = self._mcp_oauth_coordinator
        if coordinator is not None:
            coordinator.close()
        release_owner = getattr(self._prompt_queue, "release_owner", None)
        if callable(release_owner):
            with suppress(Exception):
                release_owner(self._prompt_queue_owner_id)
        if cleanup_errors:
            raise BridgeSessionCleanupError(
                "One or more IDE sessions did not close cleanly after a bounded retry."
            ) from cleanup_errors[0]

    def process_line(self, line: str) -> None:
        request_id: RequestId = None
        request: ProtocolRequest | None = None
        try:
            request = parse_request_line(line)
            request_id = request.id
            result, action = self._dispatch(request)
            self._write(response_message(request.id, result))
            if action is not None:
                action()
        except ProtocolError as e:
            self._write(
                error_message(
                    e.request_id if e.request_id is not None else request_id,
                    e.code,
                    e.message,
                )
            )
        except Exception as exc:  # noqa: BLE001 - unexpected exceptions may contain secrets
            # Only explicitly constructed ProtocolError messages are safe to
            # return to an IDE client. Provider, filesystem, SQLite, and test
            # double exceptions can embed credentials or caller-controlled
            # values in ``str(exc)``, so the catch-all boundary must not echo
            # their details onto the JSONL protocol.
            # A protected Computer Use provider failure may carry one revalidated,
            # closed-vocabulary diagnostic marker. Nothing else from the exception
            # is projected onto the protocol.
            from ..computer_use_diagnostics import protected_provider_diagnostic_marker

            marker: str | None = None
            with suppress(Exception):
                marker = protected_provider_diagnostic_marker(exc)
            self._write(
                error_message(
                    request_id,
                    "internal_error",
                    "The IDE bridge encountered an unexpected internal error."
                    + (f" {marker}" if marker else ""),
                )
            )

    def _write(self, payload: dict[str, Any]) -> None:
        with self._write_lock:
            self.stdout.write(dumps_message(payload))
            self.stdout.flush()

    def _write_transient_computer_use_request(self, payload: dict[str, Any]) -> None:
        """Write one authenticated host request without generic value rewriting.

        This path is intentionally limited to the non-replayed Computer Use
        envelope. Its argument fingerprint covers the same parsed JSON values
        delivered to the trusted host, including secret-shaped typed text.
        """

        with self._write_lock:
            self.stdout.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
            self.stdout.flush()

    def _dispatch(
        self, request: ProtocolRequest
    ) -> tuple[dict[str, Any], Callable[[], None] | None]:
        method = request.method
        # Computer Use has one host-issued capability whose exact response
        # location is checked only after its pending request binding is matched.
        # Every other request still uses the ordinary unconditional secret guard.
        if method not in {"computer.respond", "task_budget.respond"}:
            _reject_inline_secrets(request.params, request_id=request.id)
        self._reject_sensitive_media_mutation(request)
        if method == "initialize":
            return self._runtime_health_payload(), None
        if method == "health":
            return self._runtime_health_payload(), None
        if method == "getCapabilities":
            return self._runtime_capabilities_payload(), None
        if method == "bridge.shutdown":
            return {"status": "shutting_down"}, self.close
        if method == "session.create":
            result = self._session_create(request)
            created_session = self._require_session(
                str(result["session_id"]), request_id=request.id
            )
            return result, lambda: self._drain_prompt_queue(created_session)
        if method == "chat.send":
            return self._chat_send(request)
        if method == "chat.queue.list":
            return self._chat_queue_list(request), None
        if method == "chat.queue.get":
            return self._chat_queue_get(request), None
        if method == "chat.queue.delete":
            return self._chat_queue_delete(request), None
        if method == "chat.steer":
            return self._chat_steer(request), None
        if method == "chat.steer.take":
            return self._chat_steer_take(request), None
        if method == "checkpoint.list":
            return self._checkpoint_list(request), None
        if method == "checkpoint.diff":
            return self._checkpoint_diff(request), None
        if method == "checkpoint.revert":
            return self._checkpoint_revert(request), None
        if method == "checkpoint.redo":
            return self._checkpoint_redo(request), None
        if method == "checkpoint.branch":
            return self._checkpoint_branch(request), None
        if method == "session.tasks.get":
            return self._session_tasks_get(request), None
        if method == "session.tasks.replace":
            return self._session_tasks_replace(request), None
        if method in {
            "session.userQuestions.get",
            "session.userQuestions.answer",
            "session.userQuestions.touch",
        }:
            return self._session_user_questions(request), None
        if method == "session.questions.create":
            return self._session_questions_create(request), None
        if method == "session.questions.get":
            return self._session_questions_get(request), None
        if method == "session.questions.list":
            return self._session_questions_list(request), None
        if method == "session.questions.answer":
            return self._session_questions_answer(request), None
        if method == "session.questions.cancel":
            return self._session_questions_cancel(request), None
        if method == "run.start":
            return self._run_start(request)
        if method == "session.status":
            return self._session_status(request), None
        if method == "session.usage":
            return self._session_usage(request), None
        if method == "session.history":
            return self._session_history(request), None
        if method == "session.search":
            return self._session_search(request), None
        if method == "session.context":
            return self._session_context(request), None
        if method == "session.compact":
            return self._session_compact(request), None
        if method == "session.resume":
            result = self._session_resume(request)
            resumed_session = self._require_session(
                str(result["session_id"]), request_id=request.id
            )
            return result, lambda: self._resume_prompt_queue(resumed_session)
        if method == "session.fork":
            return self._session_fork(request), None
        if method == "session.images.list":
            return self._session_images_list(request), None
        if method == "session.images.add":
            return self._session_images_add(request), None
        if method == "session.images.clear":
            return self._session_images_clear(request), None
        if method == "session.setMode":
            return self._session_set_mode(request), None
        if method == "session.setModel":
            return self._session_set_model(request), None
        if method == "session.setProfile":
            return self._session_set_profile(request), None
        if method == "session.setStream":
            return self._session_set_stream(request), None
        if method == "session.setActiveWorkdir":
            return self._session_set_active_workdir(request), None
        if method == "session.modelInfo":
            return self._session_model_info(request), None
        if method == "session.personas.list":
            return self._session_personas_list(request), None
        if method == "session.persona.set":
            return self._session_persona_set(request), None
        if method == "session.subagents.status":
            return self._session_subagents_status(request), None
        if method == "session.subagents.setEnabled":
            return self._session_subagents_set_enabled(request), None
        if method == "session.trace.status":
            return self._session_trace_status(request), None
        if method == "session.trace.setLevel":
            return self._session_trace_set_level(request), None
        if method == "session.trace.listEvents":
            return self._session_trace_list_events(request), None
        if method == "session.trace.readArtifact":
            return self._session_trace_read_artifact(request), None
        if method == "session.trace.clear":
            return self._session_trace_clear(request), None
        if method == "session.terminals.list":
            return self._session_terminals_list(request), None
        if method == "session.terminals.show":
            return self._session_terminals_show(request), None
        if method == "session.terminals.kill":
            return self._session_terminals_kill(request), None
        if method == "session.terminals.clear":
            return self._session_terminals_clear(request), None
        if method == "session.clear":
            return self._session_clear(request), None
        if method == "session.cancel":
            return self._session_cancel(request), None
        if method == "approval.respond":
            return self._approval_respond(request), None
        if method == "host.action.dispatched":
            return self._host_action_dispatched(request), None
        if method == "host.action.respond":
            return self._host_action_respond(request), None
        if method == "permission.rules.list":
            return self._permission_rules_list(request), None
        if method == "permission.rules.grant":
            return self._permission_rules_grant(request), None
        if method == "permission.rules.revoke":
            return self._permission_rules_revoke(request), None
        if method == "permission.evaluate":
            return self._permission_evaluate(request), None
        if method == "permission.session.list":
            return self._permission_session_list(request), None
        if method == "permission.session.revoke":
            return self._permission_session_revoke(request), None
        if method == "code.review.start":
            return self._code_review_start(request)
        if method == "code.review.result":
            return self._code_review_job_result(request), None
        if method == "browser_start.respond":
            return self._browser_start_respond(request), None
        if method == "computer.respond":
            return self._computer_respond(request), None
        if method == "task_budget.respond":
            return self._task_budget_respond(request)
        if method == "job.status":
            return self._job_status(request), None
        if method == "session.list":
            return self._session_list(request), None
        if method == "session.getEvents":
            return self._session_get_events(request), None
        if method == "artifact.list":
            return self._artifact_list(request), None
        if method == "artifact.read":
            return self._artifact_read(request), None
        if method == "mcp.server.status":
            return self._mcp_server_status(request), None
        if method == "mcp.server.enable":
            return self._mcp_server_enable(request), None
        if method == "mcp.server.disable":
            return self._mcp_server_disable(request), None
        if method == "mcp.server.restart":
            return self._mcp_server_restart(request), None
        if method == "browser.start":
            return self._browser_start(request), None
        if method == "browser.navigate":
            return self._browser_navigate(request), None
        if method == "browser.snapshot":
            return self._browser_snapshot(request), None
        if method == "browser.screenshot":
            return self._browser_screenshot(request), None
        if method == "browser.artifact.read":
            return self._browser_artifact_read(request), None
        if method == "browser.diagnostics":
            return self._browser_diagnostics(request), None
        if method == "browser.click":
            return self._browser_click(request), None
        if method == "browser.type":
            return self._browser_type(request), None
        if method == "browser.status":
            return self._browser_status(request), None
        if method == "browser.list":
            return self._browser_list(request), None
        if method == "browser.close":
            return self._browser_close(request), None
        if method == "forge.plan":
            return self._forge_plan(request), None
        if method == "forge.plan.start":
            return self._forge_plan_start(request)
        if method == "forge.plan.result":
            return self._forge_plan_result(request), None
        if method == "forge.list":
            return self._forge_list(request), None
        if method in {"forge.open", "forge.resume"}:
            return self._forge_open(request), None
        if method == "forge.status":
            return self._forge_status(request), None
        if method == "forge.show":
            return self._forge_show(request), None
        if method == "forge.plan.getState":
            return self._forge_plan_get_state(request), None
        if method == "forge.plan.setAssistant":
            return self._forge_plan_set_assistant(request), None
        if method == "forge.plan.setGoal":
            return self._forge_plan_set_goal(request), None
        if method == "forge.plan.updateTask":
            return self._forge_plan_update_task(request), None
        if method == "forge.plan.validate":
            return self._forge_plan_validate(request), None
        if method == "forge.plan.regenerate":
            return self._forge_plan_regenerate(request), None
        if method == "forge.plan.regenerate.start":
            return self._forge_plan_regenerate_start(request)
        if method == "forge.plan.regenerate.result":
            return self._forge_plan_regenerate_job_result(request), None
        if method == "forge.review":
            return self._forge_review(request), None
        if method == "forge.attach":
            return self._forge_attach(request), None
        if method == "forge.assets.list":
            return self._forge_assets_list(request), None
        if method == "forge.assets.show":
            return self._forge_assets_show(request), None
        if method == "forge.assets.add":
            return self._forge_assets_add(request), None
        if method == "forge.assets.delete":
            return self._forge_assets_delete(request), None
        if method == "forge.assets.edit":
            return self._forge_assets_edit(request), None
        if method == "forge.assets.refresh":
            return self._forge_assets_refresh(request), None
        if method == "forge.assets.cancelPending":
            return self._forge_assets_cancel_pending(request), None
        if method == "forge.assets.checkPlan":
            return self._forge_assets_check_plan(request), None
        if method == "forge.assets.pruneLegacy":
            return self._forge_assets_prune_legacy(request), None
        if method == "forge.executePreview":
            return self._forge_execute_preview(request), None
        if method == "forge.execute":
            return self._forge_execute(request)
        if method == "forge.cancel":
            return self._forge_cancel(request), None
        if method == "forge.swarm.start":
            return self._forge_swarm_start(request)
        if method == "forge.swarm.resume":
            return self._forge_swarm_resume(request)
        if method == "forge.swarm.list":
            return self._forge_swarm_list(request), None
        if method == "forge.swarm.status":
            return self._forge_swarm_status(request), None
        if method == "forge.swarm.result":
            return self._forge_swarm_job_result(request), None
        if method == "forge.swarm.cancel":
            return self._forge_swarm_cancel(request), None
        if method == "forge.swarm.reconcile":
            return self._forge_swarm_reconcile(request), None
        if method == "forge.swarm.review":
            return self._forge_swarm_review(request), None
        if method == "forge.swarm.apply":
            return self._forge_swarm_apply(request), None
        if method == "forge.swarm.discard":
            return self._forge_swarm_discard(request), None
        if method == "forge.review.start":
            return self._forge_review_start(request)
        if method == "forge.review.result":
            return self._forge_review_job_result(request), None
        if method == "diff.list":
            return self._diff_list(request), None
        if method == "diff.get":
            return self._diff_get(request), None
        if method == "feedback.prepare":
            return self._feedback_prepare(request), None
        if method == "feedback.send":
            return self._feedback_send(request), None
        if method in MANAGEMENT_METHODS:
            return (
                handle_management_method(
                    method,
                    request.params,
                    request_id=request.id,
                    stateful_handlers={
                        "mcp.auth.login.start": self._mcp_oauth_login_start,
                        "mcp.auth.login.status": self._mcp_oauth_login_status,
                        "mcp.auth.login.cancel": self._mcp_oauth_login_cancel,
                        "mcp.auth.logout": self._mcp_oauth_logout,
                    },
                ),
                None,
            )
        raise ProtocolError(
            "method_not_found", f"Unsupported method: {method}", request_id=request.id
        )

    def _oauth_coordinator(self) -> McpOAuthIdeCoordinator:
        with self._state_lock:
            coordinator = self._mcp_oauth_coordinator
            if coordinator is None:
                coordinator = self._mcp_oauth_coordinator_factory()
                self._mcp_oauth_coordinator = coordinator
            return coordinator

    def _mcp_oauth_login_start(
        self, params: dict[str, Any], request_id: RequestId
    ) -> dict[str, Any]:
        login = prepare_mcp_oauth_login_request(params, request_id=request_id)
        try:
            status = self._oauth_coordinator().start_authorization_code(login)
        except (McpOAuthCoordinatorError, OAuthFlowStateError, OAuthValidationError) as exc:
            raise ProtocolError("mcp_oauth_error", str(exc), request_id=request_id) from exc
        return {
            **self._mcp_oauth_public_payload(status),
            "supported": True,
            "will_block": False,
            "browser_opened_by_bridge": False,
            "tokens_in_protocol_params": False,
            "authorization_code_in_protocol": False,
            "secret_values_included": False,
        }

    def _mcp_oauth_login_status(
        self, params: dict[str, Any], request_id: RequestId
    ) -> dict[str, Any]:
        login = prepare_mcp_oauth_login_request(params, request_id=request_id)
        flow_id = _required_str(params, "flow_id", request_id=request_id)
        try:
            status = self._oauth_coordinator().status(flow_id)
        except (McpOAuthCoordinatorError, OAuthFlowStateError, OAuthValidationError) as exc:
            raise ProtocolError("mcp_oauth_error", str(exc), request_id=request_id) from exc
        if status.server_id != login.server_id:
            raise ProtocolError(
                "mcp_oauth_flow_not_found",
                "MCP OAuth flow was not found for this server.",
                request_id=request_id,
            )
        return {
            **self._mcp_oauth_public_payload(status),
            "tokens_in_protocol_params": False,
            "authorization_code_in_protocol": False,
            "secret_values_included": False,
        }

    def _mcp_oauth_login_cancel(
        self, params: dict[str, Any], request_id: RequestId
    ) -> dict[str, Any]:
        login = prepare_mcp_oauth_login_request(params, request_id=request_id)
        flow_id = _required_str(params, "flow_id", request_id=request_id)
        try:
            current = self._oauth_coordinator().status(flow_id)
            if current.server_id != login.server_id:
                raise ProtocolError(
                    "mcp_oauth_flow_not_found",
                    "MCP OAuth flow was not found for this server.",
                    request_id=request_id,
                )
            status = self._oauth_coordinator().cancel(flow_id)
        except ProtocolError:
            raise
        except (McpOAuthCoordinatorError, OAuthFlowStateError, OAuthValidationError) as exc:
            raise ProtocolError("mcp_oauth_error", str(exc), request_id=request_id) from exc
        return {
            **self._mcp_oauth_public_payload(status),
            "changed": status.state.value == "cancelled",
            "tokens_in_protocol_params": False,
            "authorization_code_in_protocol": False,
            "secret_values_included": False,
        }

    @staticmethod
    def _mcp_oauth_public_payload(status: OAuthFlowStatus) -> dict[str, Any]:
        payload = status.to_public_dict()
        browser_url = payload.pop("authorization_url", None)
        if isinstance(browser_url, str) and browser_url:
            payload["browser_url"] = browser_url
        return payload

    def _mcp_oauth_logout(self, params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
        if params.get("yes") is not True and params.get("confirm") is not True:
            return {
                "changed": False,
                "action": {
                    "kind": "requires_confirmation",
                    "method": "mcp.auth.logout",
                    "reason": "clear stored MCP OAuth tokens",
                },
                "secret_values_included": False,
            }
        login = prepare_mcp_oauth_login_request(params, request_id=request_id)
        try:
            result = self._oauth_coordinator().logout(login.server_id)
        except (McpOAuthCoordinatorError, OAuthFlowStateError, OAuthValidationError) as exc:
            raise ProtocolError("mcp_oauth_error", str(exc), request_id=request_id) from exc
        return {
            "server_id": result.server_id,
            "removed": result.local_credentials_removed,
            "changed": bool(result.local_credentials_removed or result.active_flows_cancelled),
            "active_flows_cancelled": result.active_flows_cancelled,
            "remote_revocation_attempted": result.remote_revocation_attempted,
            "remote_revocation_succeeded": result.remote_revocation_succeeded,
            "error_code": result.error_code,
            "secret_values_included": False,
        }

    def _reject_sensitive_media_mutation(self, request: ProtocolRequest) -> None:
        """Fail closed on every non-read-only operation targeting a sensitive session.

        The route proof belongs to the live client, not merely the original
        session config.  Blocking all mutable session surfaces here prevents an
        interleaved protocol request from changing model/tool/workdir state in
        the interval before the worker's second route verification.
        """

        if request.method in SENSITIVE_MEDIA_ALLOWED_SESSION_METHODS:
            return
        raw_session_id = request.params.get("session_id")
        if not isinstance(raw_session_id, str) or not raw_session_id.strip():
            return
        with self._state_lock:
            session = self._sessions.get(raw_session_id.strip())
            sensitive = bool(session is not None and not session.closed and session.sensitive_media)
        if sensitive:
            raise ProtocolError(
                "sensitive_media_restricted",
                "Sensitive host-context sessions permit only the approved answer turn, cancellation, and read-only inspection.",
                request_id=request.id,
            )

    def _session_create(self, request: ProtocolRequest) -> dict[str, Any]:
        params = request.params
        mode = _mode_param(params, request_id=request.id)
        computer_use_destination = _computer_use_destination_param(
            params,
            request_id=request.id,
        )
        computer_use_host = _computer_use_host_param(
            params,
            request_id=request.id,
        )
        sensitive_media = _optional_bool(
            params,
            "sensitive_media",
            default=False,
            request_id=request.id,
        )
        reasoning_trace_events = _session_reasoning_trace_events(
            params,
            sensitive_media=sensitive_media,
            request_id=request.id,
        )
        # `sensitive_media` is the v1 wire name for the stricter host-context session.
        # The destination and fixed host tools do not require an image or model vision;
        # a bound screen image may be supplied later on chat.send when available.
        if sensitive_media != (computer_use_destination is not None):
            raise ProtocolError(
                "computer_use_destination_required",
                "sensitive_media requires one complete Computer Use destination binding.",
                request_id=request.id,
            )
        browser_start_host = _browser_start_host_param(params, request_id=request.id)
        if browser_start_host is not None and sensitive_media:
            raise ProtocolError(
                "invalid_browser_start_host",
                "Browser start is only available in ordinary sessions.",
                request_id=request.id,
            )
        if computer_use_host is not None and not sensitive_media:
            raise ProtocolError(
                "computer_use_destination_required",
                "computer_use_host requires a sensitive host-context destination binding.",
                request_id=request.id,
            )
        workspace = Path(str(params.get("workspace") or "."))
        requested_session_id = params.get("session_id")
        session_id = str(requested_session_id or make_session_id()).strip() or make_session_id()
        if requested_session_id is not None:
            _validate_session_id(session_id, request_id=request.id)
        with self._state_lock:
            existing = self._sessions.get(session_id)
            if existing is not None and not existing.closed:
                raise ProtocolError(
                    "duplicate_session_id",
                    "Session id is already active.",
                    request_id=request.id,
                )
            if existing is not None:
                self._remove_session_locked(session_id)
        cfg = clone_cfg(load_config())
        if computer_use_destination is None:
            _apply_config_overrides(cfg, params, request_id=request.id)
        else:
            _apply_computer_use_route_overrides(
                cfg,
                params,
                computer_use_destination,
                request_id=request.id,
            )
        _apply_session_sandbox_profile(cfg, params, request_id=request.id)
        if not cfg.model:
            raise ProtocolError("config_error", "Model is not set.", request_id=request.id)
        yes = _optional_bool(params, "yes", default=False, request_id=request.id)
        no_log = _optional_bool(params, "no_log", default=False, request_id=request.id)
        max_steps = _positive_int_param(
            params,
            "max_steps",
            default=int(getattr(cfg, "max_steps", 1) or 1),
            request_id=request.id,
        )
        verify_cmd = _verify_commands_param(params, request_id=request.id)
        subagents_enabled = _optional_bool_or_none(
            params,
            "subagents_enabled",
            request_id=request.id,
        )
        if sensitive_media and subagents_enabled is True:
            raise ProtocolError(
                "sensitive_media_restricted",
                "sensitive_media sessions do not permit subagents.",
                request_id=request.id,
            )
        workspace_trusted = _optional_bool(
            params,
            "workspace_trusted",
            default=False,
            request_id=request.id,
        )
        requested_host_actions = _host_capabilities_param(params, request_id=request.id)
        # A sensitive host-context session exposes only its fixed Computer Use
        # tools, so it never receives generic host-action capabilities.
        host_actions = (
            requested_host_actions if workspace_trusted and not sensitive_media else frozenset()
        )
        binding = _resolve_workspace(
            workspace,
            request_id=request.id,
            allow_subprocess=not sensitive_media,
        )
        root = binding.workspace_context.workspace_root
        host_mcp = None
        if workspace_trusted and not sensitive_media:
            try:
                host_mcp = bind_host_mcp_catalog(
                    root, normalize_host_mcp_catalog(params.get("host_mcp"))
                )
            except HostActionError as exc:
                raise ProtocolError(exc.code, exc.message, request_id=request.id) from exc
        google_mcp = []
        if workspace_trusted and not sensitive_media:
            try:
                google_mcp = bind_google_catalogs(
                    root, normalize_google_catalogs(params.get("google_mcp"))
                )
            except HostActionError as exc:
                raise ProtocolError(exc.code, exc.message, request_id=request.id) from exc
        if (GOOGLE_MCP_ACTION in host_actions) != bool(google_mcp):
            raise ProtocolError(
                "invalid_google_mcp",
                "google_mcp and mcp.google.call must be negotiated together.",
                request_id=request.id,
            )
        if mode == "readonly":
            google_mcp = [
                {**c, "tools": [t for t in c["tools"] if t["readOnly"]]} for c in google_mcp
            ]
        if (GITHUB_MCP_ACTION in host_actions) != (host_mcp is not None):
            raise ProtocolError(
                "invalid_github_mcp",
                "host_mcp and mcp.github.call must be negotiated together.",
                request_id=request.id,
            )
        discovery_actions = host_actions & (GITHUB_DISCOVERY_ACTIONS - {GITHUB_MCP_ACTION})
        if host_mcp is not None and host_mcp["protocol_version"] == 2:
            if not GITHUB_DISCOVERY_ACTIONS <= host_actions:
                raise ProtocolError(
                    "invalid_github_mcp",
                    "GitHub MCP v2 requires discover, describe and call together.",
                    request_id=request.id,
                )
        elif discovery_actions:
            raise ProtocolError(
                "invalid_github_mcp",
                "GitHub discovery actions require a v2 bootstrap.",
                request_id=request.id,
            )
        if host_mcp is not None or env_get("ALYSIS_DESKTOP_GITHUB_POLICY") is not None:
            host_actions -= {"github.repo.read", "github.pr.list", "github.pr.create"}
        if host_mcp is not None:
            if mode == "readonly" and host_mcp["protocol_version"] == 1:
                host_mcp = {
                    **host_mcp,
                    "tools": [tool for tool in host_mcp["tools"] if tool["readOnly"]],
                }
        session_log_dir = _session_log_dir_param(params, request_id=request.id)
        workspace_fence = f"wf_{uuid.uuid4().hex}"
        capability_fingerprint = _host_capability_fingerprint(
            root=root,
            workspace_trusted=workspace_trusted,
            actions=host_actions,
            host_mcp=host_mcp,
            google_mcp=google_mcp,
        )
        session_ref: dict[str, BridgeSession] = {}
        checkpoint_guard = _CheckpointToolDispatchGuard(session_ref)

        # Compose the browser service into the agent up front so model tools and
        # explicit browser.* protocol calls share one owner-scoped lifecycle.
        # The constructor is side-effect free; Chromium is launched only after
        # the corresponding approval-gated browser_start operation.
        try:
            managed_browser = self._managed_browser_factory(workspace_root=root)
        except BrowserError as exc:
            raise ProtocolError(
                "managed_browser_unavailable", str(exc), request_id=request.id
            ) from exc

        def _managed_browser_cancelled() -> bool:
            bridge_session = session_ref.get("session")
            if bridge_session is None:
                return False
            job = bridge_session.active_job
            return bool(job is not None and job.cancellation_event.is_set())

        def _host_action_handler(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return self._request_host_action(session_ref["session"], action, arguments)

        surface = ProtocolEventSurface(
            context=EventContext(session_id=session_id),
            emit=self._record_and_write_event,
            sequencer=EventSequencer(),
            approval_handler=lambda approval_request, emit_event: self._request_approval(
                session_ref["session"],
                approval_request,
                emit_event,
            ),
            standing_approval_handler=lambda approval_request: self._standing_approval(
                session_ref["session"],
                approval_request,
            ),
            semantic_activity_events=True,
            reasoning_trace_events=reasoning_trace_events,
        )
        surface.host_action_approval_protocol = int(
            params.get("host_capabilities", {}).get("approval_protocol", 0)
        )
        console = make_console(file=io.StringIO(), force_terminal=False, width=120)
        if _optional_bool(params, "user_questions", default=False, request_id=request.id):
            if sensitive_media:
                raise ProtocolError(
                    "sensitive_media_restricted",
                    "Questions are unavailable in sensitive sessions.",
                    request_id=request.id,
                )
            from ..user_questions import UserQuestionBroker

            surface.host_managed_user_questions = True
            surface.user_questions = UserQuestionBroker(is_cancelled=_managed_browser_cancelled)
        agent_session: Any | None = None
        try:
            with task_budget_setup(
                computer_use_host is not None and computer_use_host.task_budget is not None
            ):
                agent_session = self._create_session(
                    cfg=cfg,
                    root=root,
                    mode=mode,
                    runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
                    yes=yes,
                    max_steps=max_steps,
                    no_log=no_log,
                    api_key_override=None,
                    console=console,
                    surface=surface,
                    non_interactive=True,
                    enable_chat_turn_step_budget=True,
                    chat_turn_fixed_override=(
                        max_steps if _param_is_present(params, "max_steps") else None
                    ),
                    verify_cmd=verify_cmd,
                    subagents_enabled=subagents_enabled,
                    workspace_binding=binding,
                    # Sensitive sessions build only the fixed Computer Use tool
                    # surface; they never receive the managed browser service.
                    managed_browser_service=None if sensitive_media else managed_browser,
                    managed_browser_owner_id=session_id,
                    managed_browser_cancel_check=_managed_browser_cancelled,
                    host_action_handler=_host_action_handler if host_actions else None,
                    host_action_capabilities=host_actions,
                    **({"host_mcp": host_mcp} if host_mcp is not None else {}),
                    **({"google_mcp": google_mcp} if google_mcp else {}),
                    session_log_dir_override=session_log_dir,
                    session_id_override=session_id,
                    tool_dispatch_guard=checkpoint_guard,
                    **(
                        {
                            "computer_discovery": browser_start_host.get("discovery"),
                            "browser_start_host_request": lambda url: self._request_browser_start(
                                session_ref["session"], url
                            ),
                        }
                        if browser_start_host is not None
                        else {}
                    ),
                    # AgentSession owns the complete startup restriction (hooks, MCP,
                    # plugins/custom tools, skills, subagents, power/background probes).
                    # The bridge passes this before the session can inspect artifact paths.
                    **({"sensitive_media": True} if sensitive_media else {}),
                    **(
                        {
                            "trusted_system_prompt_override": COMPUTER_USE_SYSTEM_PROMPT
                            + (
                                COMPUTER_USE_VERIFICATION_PROMPT
                                if computer_use_host.verification_available
                                else ""
                            )
                            + (
                                COMPUTER_USE_FILE_PROMPT
                                if computer_use_host.files_available
                                else ""
                            )
                            + (
                                COMPUTER_USE_UPLOAD_PROMPT
                                if computer_use_host.uploads_available
                                else ""
                            ),
                            "computer_use_host_request": (
                                lambda tool_name, arguments: self._request_computer_use(
                                    session_ref["session"],
                                    tool_name,
                                    arguments,
                                )
                            ),
                            "computer_use_response_mode": computer_use_host.response_mode,
                            "computer_observation_available": computer_use_host.observation_version
                            == 1,
                            "computer_use_verification_available": computer_use_host.verification_available,
                            "computer_use_files_available": computer_use_host.files_available,
                            "computer_use_uploads_available": computer_use_host.uploads_available,
                            "computer_use_upload_objective": computer_use_host.upload_objective,
                        }
                        if computer_use_host is not None
                        else {}
                    ),
                )
            active_workdir = _optional_str(params, "active_workdir", request_id=request.id)
            if active_workdir is None:
                active_workdir = _optional_str(
                    params,
                    "active_workdir_relpath",
                    request_id=request.id,
                )
            if active_workdir is not None:
                set_session_active_workdir(agent_session, active_workdir, source="ide_protocol")
            persona_source = "config"
            if not sensitive_media:
                # Custom personas are project-supplied prompt overlays; the answer-only
                # sensitive host-context session never loads them.
                _attach_session_persona_registry(agent_session, cfg=cfg, root=root)
                # Start on the persona `alysis chat` would, with its mode, write scope
                # and model role in effect; a narrowing persona lowers the mode returned.
                persona_source = _apply_bridge_session_persona(agent_session)
                mode = str(getattr(agent_session, "mode", mode) or mode)
            actual_destination = (
                _actual_computer_use_destination(agent_session, request_id=request.id)
                if computer_use_destination is not None
                else None
            )
            if computer_use_destination is not None and not _same_computer_use_destination(
                computer_use_destination,
                actual_destination,
            ):
                raise ProtocolError(
                    "computer_use_destination_mismatch",
                    "The live CLI client does not match the approved Computer Use destination.",
                    request_id=request.id,
                )
        except Exception as exc:
            if agent_session is not None:
                with suppress(Exception):
                    agent_session.close()
            with suppress(Exception):
                managed_browser.close_all()
            with self._state_lock:
                self._remove_session_locked(session_id)
            if isinstance(exc, RequiredToolCallingUnsupportedError):
                raise ProtocolError(
                    "computer_use_tool_calling_unsupported",
                    str(exc),
                    request_id=request.id,
                ) from exc
            if isinstance(exc, ConfigError):
                raise ProtocolError("config_error", str(exc), request_id=request.id) from exc
            if isinstance(exc, ProviderAuthError):
                # Engine-authored text ("Reconnect ChatGPT to authorize Alysis Code for plan
                # usage.", "The selected reasoning effort is not supported by this provider
                # and model."): every ProviderAuthError message is a fixed sentence or an
                # engine value, never provider or credential content, so the host can show
                # the user what to do instead of "an unexpected internal error".
                raise ProtocolError("provider_auth_error", str(exc), request_id=request.id) from exc
            raise
        if computer_use_host is not None and computer_use_host.task_budget is not None:
            try:
                _assert_task_budget_client_supported(agent_session)
            except TaskBudgetRefused:
                agent_session.close()
                with suppress(Exception):
                    managed_browser.close_all()
                raise ProtocolError(
                    "task_budget_unavailable",
                    "This protected provider configuration does not support the exact task budget contract.",
                    request_id=request.id,
                ) from None
        store = getattr(agent_session, "store", None)
        roots: list[ArtifactRoot] = []
        artifact_root = getattr(store, "session_artifact_root", None)
        if artifact_root is not None:
            roots.append(ArtifactRoot("session", Path(artifact_root)))
        tool_output_offloader = getattr(agent_session, "tool_output_offloader", None)
        offload_root = getattr(tool_output_offloader, "artifact_root", None)
        if offload_root is not None and (
            artifact_root is None or Path(offload_root).resolve() != Path(artifact_root).resolve()
        ):
            roots.append(ArtifactRoot("tool_output", Path(offload_root)))
        bridge_session = BridgeSession(
            session_id=session_id,
            root=root,
            mode=str(getattr(agent_session, "mode", mode)),
            persona_source=persona_source,
            surface=surface,
            agent_session=agent_session,
            artifact_store=ArtifactStore(roots),
            checkpoint_guard=checkpoint_guard,
            managed_browser=managed_browser,
            workspace_trusted=workspace_trusted,
            host_actions=host_actions,
            host_approval_protocol=int(
                params.get("host_capabilities", {}).get("approval_protocol", 0)
            ),
            host_mcp=host_mcp,
            google_mcp=google_mcp,
            workspace_fence=workspace_fence,
            host_capability_fingerprint=capability_fingerprint,
            sensitive_media=sensitive_media,
            computer_use_destination=actual_destination,
            computer_use_host=computer_use_host,
            browser_start_host=browser_start_host,
        )
        session_ref["session"] = bridge_session
        with self._state_lock:
            self._sessions[session_id] = bridge_session
        return {
            "session_id": session_id,
            "workspace_root": os.fspath(root),
            "mode": mode,
            "sandbox_mode": resolve_shell_sandbox_settings(cfg).mode,
            "session_dir": os.fspath(session_log_dir) if session_log_dir is not None else None,
            "reasoning_trace": reasoning_trace_events,
            **(
                {"browser_start_host": dict(browser_start_host)}
                if browser_start_host is not None
                else {}
            ),
            **(
                {"computer_use_destination": actual_destination.to_protocol()}
                if actual_destination is not None
                else {}
            ),
            **(
                {"computer_use_host": computer_use_host.to_protocol()}
                if computer_use_host is not None
                else {}
            ),
            "host_actions": _host_actions_session_payload(
                bridge_session,
                request_timeout_seconds=self._host_action_timeout_seconds,
            ),
            **({"host_mcp": host_mcp_session_payload(host_mcp)} if host_mcp is not None else {}),
            **({"google_mcp": google_session_payload(google_mcp)} if google_mcp else {}),
        }

    def _live_mcp_manager(
        self,
        session: BridgeSession,
        *,
        request_id: RequestId,
    ) -> McpManager:
        manager = getattr(session.agent_session, "mcp_manager", None)
        if not isinstance(manager, McpManager):
            raise ProtocolError(
                "mcp_live_manager_unavailable",
                "The owning IDE session does not expose a live MCP manager.",
                request_id=request_id,
            )
        manager_session_id = str(manager.session_id or "").strip()
        if manager_session_id != session.session_id:
            raise ProtocolError(
                "mcp_owner_mismatch",
                "The live MCP manager is not owned by the requested IDE session.",
                request_id=request_id,
            )
        if manager.workspace_root.resolve() != session.root.resolve():
            raise ProtocolError(
                "mcp_owner_mismatch",
                "The live MCP manager is not bound to the requested IDE workspace.",
                request_id=request_id,
            )
        return manager

    def _mcp_server_request_context(
        self,
        request: ProtocolRequest,
        *,
        mutating: bool,
    ) -> tuple[BridgeSession, McpManager, str]:
        if mutating:
            self._require_workspace_trusted(request)
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        server_id = _required_str(request.params, "server_id", request_id=request.id)
        if mutating:
            with self._state_lock:
                _reconcile_session_job_state(session)
                if _job_is_active(session.active_job):
                    raise ProtocolError(
                        "session_busy",
                        "MCP server lifecycle changes require an idle session.",
                        request_id=request.id,
                    )
        return session, self._live_mcp_manager(session, request_id=request.id), server_id

    def _mcp_server_status(self, request: ProtocolRequest) -> dict[str, Any]:
        session, manager, server_id = self._mcp_server_request_context(
            request,
            mutating=False,
        )
        try:
            status = manager.server_lifecycle_status(server_id=server_id)
        except (ConfigError, RuntimeError) as exc:
            raise _mcp_lifecycle_protocol_error(exc, request_id=request.id) from exc
        return {**status, "session_id": session.session_id}

    def _mcp_server_enable(self, request: ProtocolRequest) -> dict[str, Any]:
        session, manager, server_id = self._mcp_server_request_context(
            request,
            mutating=True,
        )
        try:
            result = manager.enable_server(server_id=server_id)
        except (ConfigError, RuntimeError) as exc:
            raise _mcp_lifecycle_protocol_error(exc, request_id=request.id) from exc
        return {**result, "session_id": session.session_id}

    def _mcp_server_disable(self, request: ProtocolRequest) -> dict[str, Any]:
        session, manager, server_id = self._mcp_server_request_context(
            request,
            mutating=True,
        )
        try:
            result = manager.disable_server(server_id=server_id)
        except (ConfigError, RuntimeError) as exc:
            raise _mcp_lifecycle_protocol_error(exc, request_id=request.id) from exc
        return {**result, "session_id": session.session_id}

    def _mcp_server_restart(self, request: ProtocolRequest) -> dict[str, Any]:
        session, manager, server_id = self._mcp_server_request_context(
            request,
            mutating=True,
        )
        try:
            result = manager.restart_server(server_id=server_id)
        except (ConfigError, RuntimeError) as exc:
            raise _mcp_lifecycle_protocol_error(exc, request_id=request.id) from exc
        return {**result, "session_id": session.session_id}

    def _chat_send(
        self,
        request: ProtocolRequest,
        *,
        run_start_fingerprint: str | None = None,
    ) -> tuple[dict[str, Any], Callable[[], None]]:
        params = request.params
        _reject_unsupported_chat_send_fields(params, request_id=request.id)
        session = self._require_session(
            _required_str(params, "session_id", request_id=request.id), request_id=request.id
        )
        message = _message_param(params, request_id=request.id)
        if not message:
            raise ProtocolError("invalid_request", "message is required.", request_id=request.id)
        skill = validate_skill_invocation(
            session.agent_session,
            params.get("skill"),
            sensitive_media=session.sensitive_media,
            request_id=request.id,
        )
        if session.sensitive_media:
            return self._chat_send_sensitive_turn(request, session, message)
        if "host_image" in params:
            raise ProtocolError(
                "host_image_binding_unexpected",
                "host_image is only valid for a sensitive screen-image session.",
                request_id=request.id,
            )
        turn_image_paths = _image_paths_param(
            params, workspace_root=session.root, request_id=request.id
        )
        try:
            context_ignore = _make_context_ignore_predicate(session.root)
            context_bundle = sanitize_context_blocks(
                params.get("context_blocks") or [],
                workspace_roots=[session.root],
                is_ignored=context_ignore,
                path_policy=lambda path, _kind: (
                    "deny" if classify_sensitive_path(path).sensitive else "allow"
                ),
            )
        except ContextValidationError as exc:
            raise ProtocolError(exc.code, exc.message, request_id=request.id) from exc
        raw_idempotency_key = params.get("idempotency_key")
        if raw_idempotency_key is not None and not isinstance(raw_idempotency_key, str):
            raise ProtocolError(
                "invalid_field", "idempotency_key must be a string.", request_id=request.id
            )
        idempotency_key = str(raw_idempotency_key or uuid.uuid4().hex).strip()
        with self._state_lock:
            _reconcile_session_job_state(session)
            basket_image_paths = list(session.pending_images)
            image_paths = _dedupe_paths([*basket_image_paths, *turn_image_paths])
        queue_payload: dict[str, Any] = {
            "message": message,
            "image_paths": image_paths,
            "basket_image_paths": basket_image_paths,
            "context": context_bundle.to_dict(),
            "context_prompt": context_bundle.to_prompt(),
        }
        if skill is not None:
            queue_payload["skill"] = skill
        if run_start_fingerprint is not None:
            queue_payload["run_start_fingerprint"] = run_start_fingerprint
        try:
            queued = self._prompt_queue.enqueue(
                session_id=session.session_id,
                idempotency_key=idempotency_key,
                payload=queue_payload,
            )
        except PromptQueueError as exc:
            raise ProtocolError("prompt_queue_error", str(exc), request_id=request.id) from exc
        item = queued.item
        with self._state_lock:
            if queued.created and basket_image_paths:
                # The basket belongs to the next accepted prompt, not the next
                # completed prompt. Reserving it here prevents queued follow-ups
                # from inheriting the same images while preserving retry safety.
                _remove_pending_images(session, basket_image_paths)
            _reconcile_session_job_state(session)
            job = self._jobs.get(item.prompt_id)
            if job is None:
                self._prune_terminal_jobs_locked(reserve=1)
                job = BridgeJob(
                    job_id=item.prompt_id,
                    session_id=session.session_id,
                    created_at=_timestamp_iso(item.created_at),
                    kind="session_turn",
                    status=item.state.value,
                    prompt_id=item.prompt_id,
                    prompt_sequence=item.sequence,
                )
                self._jobs[job.job_id] = job
            idle = not _job_is_active(session.active_job)

        def _start() -> None:
            session.surface.emit_activity(
                ActivityEvent(
                    activity_id=f"prompt-{item.prompt_id}",
                    kind="plan",
                    operation="queue_prompt",
                    display_title="Queued follow-up",
                    status="running" if idle else "queued",
                    summary=f"Prompt {item.sequence} is {'starting' if idle else 'queued'}.",
                )
            )
            if idle:
                self._drain_prompt_queue(session)
            if not queued.created and item.state is PromptState.RUNNING:
                self._schedule_prompt_reconciliation(session, job)

        response_status = item.state.value
        if item.state is PromptState.PENDING:
            response_status = "started" if idle else "queued"
        elif item.state is PromptState.RUNNING:
            response_status = "running"
        return {
            "session_id": session.session_id,
            "job_id": job.job_id,
            "prompt_id": item.prompt_id,
            "queue_sequence": item.sequence,
            "status": response_status,
            "created": queued.created,
        }, _start

    def _chat_queue_list(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        limit = _positive_int_param(
            request.params, "limit", default=100, upper=500, request_id=request.id
        )
        after_sequence = _non_negative_int_param(
            request.params, "after_sequence", default=0, request_id=request.id
        )
        try:
            items = self._prompt_queue.list(
                session_id=session.session_id,
                states=request.params.get("states"),
                limit=limit,
                after_sequence=after_sequence,
            )
        except PromptQueueError as exc:
            raise ProtocolError("prompt_queue_error", str(exc), request_id=request.id) from exc
        return {
            "session_id": session.session_id,
            "items": [_prompt_queue_item_payload(item) for item in items],
            "next_sequence": items[-1].sequence if items else after_sequence,
        }

    def _chat_send_sensitive_turn(
        self,
        request: ProtocolRequest,
        session: BridgeSession,
        message: str,
    ) -> tuple[dict[str, Any], Callable[[], None]]:
        """Start the one-shot, answer-only turn of a sensitive host-context session.

        The host-image descriptor and its validated bytes are a transient,
        worker-local capability. They are never written to the durable prompt
        queue, so this turn bypasses it and runs as a single direct job.
        """

        params = request.params
        if params.get("context_blocks"):
            raise ProtocolError(
                "sensitive_media_restricted",
                "Sensitive host-context sessions do not accept context blocks.",
                request_id=request.id,
            )
        if session.sensitive_media:
            _assert_live_computer_use_destination(session, request_id=request.id)
            with self._state_lock:
                _reconcile_session_job_state(session)
                if _job_is_active(session.active_job):
                    raise ProtocolError(
                        "session_busy",
                        "Session already has a running job.",
                        request_id=request.id,
                    )
                if session.host_image_descriptor_consumed and "host_image" in params:
                    raise ProtocolError(
                        "host_image_binding_consumed",
                        "The sensitive screen-image descriptor was already used.",
                        request_id=request.id,
                    )

        # Start the session-local redactor before descriptor/path validation. A
        # validation or thread-start failure must not be able to echo a staged
        # host path or a provider-formatted data URL into bridge diagnostics.
        redactor_started = False
        if session.sensitive_media and "host_image" in params:
            _activate_bridge_host_screen_redactor(session)
            redactor_started = True
        try:
            host_image_binding = _host_image_binding_param(params, request_id=request.id)
            if session.sensitive_media:
                if host_image_binding is None and (
                    "images" in params or "image_paths" in params or "host_image" in params
                ):
                    raise ProtocolError(
                        "host_image_binding_missing",
                        "The sensitive screen-image turn requires its one-shot host image descriptor.",
                        request_id=request.id,
                    )
                if host_image_binding is not None and session.host_image_descriptor_consumed:
                    raise ProtocolError(
                        "host_image_binding_consumed",
                        "The sensitive screen-image descriptor was already used.",
                        request_id=request.id,
                    )
            elif host_image_binding is not None:
                raise ProtocolError(
                    "host_image_binding_unexpected",
                    "host_image is only valid for a sensitive screen-image session.",
                    request_id=request.id,
                )
            if host_image_binding is not None:
                _extend_bridge_host_screen_redactor(session, [host_image_binding.root])
            # ContextVars do not cross the worker-thread boundary. This scope exists
            # only for parsing the request; the same binding is installed again around
            # the exact worker turn below and is never placed on session/environment.
            image_context = (
                use_ide_host_image_binding(host_image_binding)
                if host_image_binding is not None
                else nullcontext()
            )
            prepared_image = None
            with image_context:
                turn_image_paths = _image_paths_param(
                    params, workspace_root=session.root, request_id=request.id
                )
                if host_image_binding is not None and len(turn_image_paths) == 1:
                    image = validate_ide_host_image(
                        turn_image_paths[0],
                        max_bytes=MAX_IDE_IMAGE_BYTES,
                        supported_mime_types=SUPPORTED_IDE_IMAGE_MIME_TYPES,
                    )
                    prepared_image = (
                        image,
                        read_validated_host_image(image, max_bytes=MAX_IDE_IMAGE_BYTES),
                    )
            if host_image_binding is not None:
                _extend_bridge_host_screen_redactor(session, turn_image_paths)
            if host_image_binding is not None and len(turn_image_paths) != 1:
                raise ProtocolError(
                    "host_image_binding_invalid",
                    "A sensitive screen-image turn requires exactly one host image.",
                    request_id=request.id,
                )
            with self._state_lock:
                _reconcile_session_job_state(session)
                if _job_is_active(session.active_job):
                    raise ProtocolError(
                        "session_busy", "Session already has a running job.", request_id=request.id
                    )
                basket_image_paths = list(session.pending_images)
                if session.sensitive_media and basket_image_paths:
                    raise ProtocolError(
                        "sensitive_media_restricted",
                        "Sensitive screen-image sessions do not keep an image basket.",
                        request_id=request.id,
                    )
                image_paths = _dedupe_paths([*basket_image_paths, *turn_image_paths])
                job = BridgeJob(
                    job_id=_make_job_id(),
                    session_id=session.session_id,
                    created_at=_now_iso(),
                    kind="session_turn",
                    host_image_binding=host_image_binding,
                    prepared_host_image=prepared_image,
                )
                if host_image_binding is not None:
                    session.host_image_descriptor_consumed = True
                self._register_job_locked(session, job)
        except Exception:
            if redactor_started:
                _clear_bridge_host_screen_redactor(session)
            raise

        def _start() -> None:
            try:
                thread = threading.Thread(
                    target=self._run_session_turn,
                    args=(session, job, message, image_paths, basket_image_paths),
                    name=f"alysis-ide-{job.job_id}",
                    daemon=True,
                )
            except Exception as exc:  # noqa: BLE001 - thread allocation may fail under pressure.
                safe_error = _redact_bridge_error(
                    session,
                    f"job_start_failed: {exc}",
                    host_image_binding=job.host_image_binding,
                    image_paths=image_paths,
                )
                with self._state_lock:
                    _mark_job_completed(job, status="failed", exit_code=1, error=safe_error)
                    _reconcile_session_job_state(session)
                    self._prune_terminal_jobs_locked()
                session.surface.emit_error("job_start_failed", safe_error, False)
                _clear_bridge_host_screen_redactor(session)
                return
            self._start_job_thread(session, job, thread)

        return {
            "session_id": session.session_id,
            "job_id": job.job_id,
            "status": "started",
            "host_image_consumed": prepared_image is not None,
        }, _start

    def _chat_queue_get(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        prompt_id = _required_str(request.params, "prompt_id", request_id=request.id)
        try:
            item = self._prompt_queue.get(session_id=session.session_id, prompt_id=prompt_id)
        except PromptQueueError as exc:
            raise ProtocolError("prompt_queue_error", str(exc), request_id=request.id) from exc
        if item is None:
            raise ProtocolError(
                "prompt_not_found", "Queued prompt was not found.", request_id=request.id
            )
        return _prompt_queue_item_payload(item, include_preview=True)

    def _chat_queue_delete(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        prompt_id = _required_str(request.params, "prompt_id", request_id=request.id)
        try:
            item = self._prompt_queue.delete_pending(
                session_id=session.session_id, prompt_id=prompt_id
            )
        except PromptQueueError as exc:
            raise ProtocolError("prompt_queue_error", str(exc), request_id=request.id) from exc
        with self._state_lock:
            job = self._jobs.get(prompt_id)
            if job is not None and job.status == "queued":
                _mark_job_completed(job, status="cancelled", exit_code=130, error="queue_deleted")
        session.surface.emit_activity(
            ActivityEvent(
                activity_id=f"prompt-{prompt_id}",
                kind="plan",
                operation="delete_queued_prompt",
                display_title="Removed queued follow-up",
                status="cancelled",
            )
        )
        return _prompt_queue_item_payload(item)

    def _chat_steer(self, request: ProtocolRequest) -> dict[str, Any]:
        """Add a user message to the running turn, read at its next step.

        This feeds the same steering inbox the TUI uses (``agent/steering.py``).
        The turn delivers it as a durable user message and logs
        ``steer_message_delivered``. Without a running turn there is nothing to
        steer, so the host sends the message with ``chat.send`` instead.
        """
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        message = _message_param(request.params, request_id=request.id)
        with self._state_lock:
            _reconcile_session_job_state(session)
            job = session.active_job
            if not _job_is_active(job) or job is None:
                raise ProtocolError(
                    "session_idle",
                    "No turn is running to steer; send the message with chat.send.",
                    request_id=request.id,
                )
            if job.kind != "session_turn":
                raise ProtocolError(
                    "session_busy",
                    "The session is running a job that does not take messages.",
                    request_id=request.id,
                )
            inbox = steer_inbox_for(session.agent_session, create=True)
            if inbox is None:
                raise ProtocolError(
                    "steering_unavailable",
                    "This session cannot take messages during a turn.",
                    request_id=request.id,
                )
            dropped_before = inbox.dropped_count()
            queued = inbox.send(message)
            return {
                "session_id": session.session_id,
                "job_id": job.job_id,
                "accepted": True,
                "pending": inbox.pending_count(),
                # Older waiting messages this call pushed out (the inbox keeps the newest 16).
                "dropped": inbox.dropped_count() - dropped_before,
                # The inbox shortens long messages; the turn reads the shortened text.
                "truncated": queued != message,
            }

    def _chat_steer_take(self, request: ProtocolRequest) -> dict[str, Any]:
        """Return the steering messages finished turns did not read, oldest first.

        A turn reads steering only between its steps, so a message can arrive
        after its last step. The bridge holds such messages when the turn ends,
        so a later turn never reads them; the TUI starts them as a turn of their
        own, and this hands them to the host to do the same. A running turn's
        own messages stay with it.
        """
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        with self._state_lock:
            _reconcile_session_job_state(session)
            messages = list(session.unread_steers)
            session.unread_steers.clear()
            if not _job_is_active(session.active_job):
                inbox = steer_inbox_for(session.agent_session)
                if inbox is not None:
                    messages.extend(inbox.drain())
        return {"session_id": session.session_id, "messages": messages}

    def _feedback_prepare(self, request: ProtocolRequest) -> dict[str, Any]:
        """Build a private report for the user to review; nothing is sent yet.

        The same report the TUI's "Send to Alysis" builds (``feedback_delivery``):
        the redacted description, the engine version and, only when asked, the
        last messages of the session plus OS and Python version. A session that is
        not open in this bridge is read from its retained log (``workspace`` and an
        optional ``session_dir`` as in ``session.create``); without a session_id the
        report has no conversation. The exact
        bytes stay in this bridge until ``feedback.send``. ``report`` is that payload as
        an object for the host to show, after the bridge's secret redaction; the bytes
        sent are the redacted ones too.
        """
        params = request.params
        feedback = params.get("feedback")
        if not isinstance(feedback, str):
            raise ProtocolError(
                "invalid_field", "feedback must be a string.", request_id=request.id
            )
        include_diagnostics = _optional_bool(
            params, "include_diagnostics", default=False, request_id=request.id
        )
        session_id = _optional_str(params, "session_id", request_id=request.id)
        with self._state_lock:
            live = self._sessions.get(session_id) if session_id else None
        active_session: Any = None
        if session_id and live is not None and not live.closed:
            session = self._require_session(session_id, request_id=request.id)
            root, active_session = session.root, session.agent_session
        else:
            workspace = _required_str(params, "workspace", request_id=request.id)
            binding = _resolve_workspace(Path(workspace), request_id=request.id)
            root = binding.workspace_context.workspace_root
            if session_id and include_diagnostics:
                active_session = _RetainedFeedbackSession(
                    _retained_session_messages(params, session_id, request_id=request.id)
                )
        try:
            prepared = feedback_delivery.prepare_feedback(
                workspace_root=root,
                feedback_text=feedback,
                include_diagnostics=include_diagnostics,
                active_session=active_session,
            )
        except FeedbackReportError as exc:
            raise ProtocolError("feedback_invalid", str(exc), request_id=request.id) from exc
        # Every result passes the bridge's redaction before the host shows it. Send exactly
        # that text, so a span the host showed as hidden never leaves the computer.
        report = json.loads(prepared.body)
        shown = redact_secrets(report)
        if shown != report:
            body = json.dumps(shown, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(body) > feedback_delivery.MAX_REQUEST_BYTES:
                raise ProtocolError(
                    "feedback_invalid",
                    "Report is too large. Turn off diagnostics.",
                    request_id=request.id,
                )
            prepared = feedback_delivery.PreparedFeedback(
                prepared.report_id, prepared.endpoint, body
            )
        with self._state_lock:
            self._prepared_feedback[prepared.report_id] = prepared
            while len(self._prepared_feedback) > MAX_PREPARED_FEEDBACK:
                self._prepared_feedback.popitem(last=False)
        return {
            "report_id": prepared.report_id,
            "report": shown,
            "include_diagnostics": include_diagnostics,
            "signed_in": feedback_delivery.has_alysis_sign_in(),
        }

    def _feedback_send(self, request: ProtocolRequest) -> dict[str, Any]:
        """Send exactly the bytes ``feedback.prepare`` reviewed for this report_id.

        Signed in, the report is tied to the Alysis account; otherwise it goes
        anonymously. A retry with the same report_id is safe: the server returns
        the same receipt and never stores it twice. Each network step waits at most
        15 s. A refusal names its reason (rate limit, sign-in, size) in the error code.
        """
        report_id = _required_str(request.params, "report_id", request_id=request.id)
        with self._state_lock:
            prepared = self._prepared_feedback.get(report_id)
        if prepared is None:
            raise ProtocolError(
                "feedback_not_prepared",
                "This report is no longer prepared. Prepare it again, then send.",
                request_id=request.id,
            )
        try:
            receipt = feedback_delivery.send_feedback(prepared)
        except FeedbackReportError as exc:
            raise ProtocolError(
                _feedback_delivery_code(exc), str(exc), request_id=request.id
            ) from exc
        return {
            "report_id": receipt.report_id,
            "created_at": receipt.created_at,
            "anonymous": receipt.anonymous,
        }

    def _checkpoint_list(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        limit = _positive_int_param(
            request.params, "limit", default=100, upper=500, request_id=request.id
        )
        try:
            ledger = self._change_ledger_for_session(session)
            ledger.ensure_baseline(session.session_id)
            checkpoints = ledger.list(session.session_id, limit=limit)
        except ChangeLedgerError as exc:
            raise ProtocolError("checkpoint_error", str(exc), request_id=request.id) from exc
        return {
            "session_id": session.session_id,
            "checkpoints": [_checkpoint_payload(item) for item in checkpoints],
        }

    def _checkpoint_diff(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        checkpoint_id = _required_str(request.params, "checkpoint_id", request_id=request.id)
        max_bytes = _positive_int_param(
            request.params,
            "max_bytes",
            default=2 * 1024 * 1024,
            upper=16 * 1024 * 1024,
            request_id=request.id,
        )
        try:
            result = self._change_ledger_for_session(session).diff(
                session.session_id, checkpoint_id, max_bytes=max_bytes
            )
        except ChangeLedgerError as exc:
            raise ProtocolError("checkpoint_error", str(exc), request_id=request.id) from exc
        return {"session_id": session.session_id, **result}

    def _checkpoint_revert(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._checkpoint_mutation_session(request)
        checkpoint_id = _required_str(request.params, "checkpoint_id", request_id=request.id)
        try:
            result = self._change_ledger_for_session(session).revert(
                session.session_id, checkpoint_id
            )
        except StaleWorkspaceError as exc:
            raise ProtocolError("stale_workspace", str(exc), request_id=request.id) from exc
        except ChangeLedgerError as exc:
            raise ProtocolError("checkpoint_error", str(exc), request_id=request.id) from exc
        session.surface.emit_activity(
            ActivityEvent(
                activity_id=f"checkpoint-{result.checkpoint_id}",
                kind="edit",
                operation="revert_checkpoint",
                display_title="Reverted agent changes",
                status="succeeded",
                files=tuple(record.path for record in result.changes),
            )
        )
        return {"session_id": session.session_id, "checkpoint": _checkpoint_payload(result)}

    def _checkpoint_redo(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._checkpoint_mutation_session(request)
        try:
            result = self._change_ledger_for_session(session).redo(
                session.session_id,
                expected_checkpoint_id=_optional_str(
                    request.params, "expected_checkpoint_id", request_id=request.id
                ),
            )
        except StaleWorkspaceError as exc:
            raise ProtocolError("stale_workspace", str(exc), request_id=request.id) from exc
        except ChangeLedgerError as exc:
            raise ProtocolError("checkpoint_error", str(exc), request_id=request.id) from exc
        session.surface.emit_activity(
            ActivityEvent(
                activity_id=f"checkpoint-{result.checkpoint_id}",
                kind="edit",
                operation="redo_checkpoint",
                display_title="Restored agent changes",
                status="succeeded",
                files=tuple(record.path for record in result.changes),
            )
        )
        return {"session_id": session.session_id, "checkpoint": _checkpoint_payload(result)}

    def _checkpoint_branch(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        checkpoint_id = _required_str(request.params, "checkpoint_id", request_id=request.id)
        name = _required_str(request.params, "name", request_id=request.id)
        try:
            ref = self._change_ledger_for_session(session).create_branch(
                session.session_id, name, checkpoint_id
            )
        except ChangeLedgerError as exc:
            raise ProtocolError("checkpoint_error", str(exc), request_id=request.id) from exc
        return {
            "session_id": session.session_id,
            "checkpoint_id": checkpoint_id,
            "ref": ref,
        }

    def _session_tasks_get(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        target_session_id = (
            _optional_str(request.params, "target_session_id", request_id=request.id)
            or session.session_id
        )
        try:
            ledger = self._structured_state_for_session(
                session, request_id=request.id
            ).get_task_ledger(session_id=target_session_id)
        except StructuredStateError as exc:
            raise ProtocolError("structured_state_error", str(exc), request_id=request.id) from exc
        return {
            "session_id": session.session_id,
            "target_session_id": target_session_id,
            **ledger.public_payload(),
        }

    def _session_tasks_replace(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        tasks = request.params.get("tasks")
        if not isinstance(tasks, list):
            raise ProtocolError("invalid_field", "tasks must be an array.", request_id=request.id)
        expected_revision = _non_negative_int_param(
            request.params, "expected_revision", default=0, request_id=request.id
        )
        try:
            ledger = self._structured_state_for_session(
                session, request_id=request.id
            ).replace_tasks(
                session_id=session.session_id,
                expected_revision=expected_revision,
                tasks=tasks,
            )
        except TaskRevisionConflict as exc:
            return {
                "session_id": session.session_id,
                "updated": False,
                "conflict": True,
                "current_revision": exc.current_revision,
            }
        except StructuredStateError as exc:
            raise ProtocolError("structured_state_error", str(exc), request_id=request.id) from exc
        session.surface.emit_activity(
            ActivityEvent(
                activity_id=f"tasks-{session.session_id}-{ledger.revision}",
                kind="plan",
                operation="update_task_state",
                display_title="Updated task progress",
                status="succeeded",
                summary=f"Task state revision {ledger.revision} contains {len(ledger.tasks)} item(s).",
            )
        )
        return {
            "session_id": session.session_id,
            "updated": True,
            "conflict": False,
            **ledger.public_payload(),
        }

    def _session_user_questions(self, request: ProtocolRequest) -> dict[str, Any]:
        from ..user_questions import question_broker

        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        job_id = _required_str(request.params, "job_id", request_id=request.id)
        job = session.active_job or (
            session.last_job if request.method == "session.userQuestions.get" else None
        )
        broker = question_broker(session.surface)
        if broker is None or job is None or job.job_id != job_id:
            raise ProtocolError(
                "question_unavailable",
                "This question no longer belongs to the active turn.",
                request_id=request.id,
            )
        if request.method != "session.userQuestions.get":
            if not _job_is_active(job) or job.cancellation_event.is_set():
                raise ProtocolError(
                    "question_unavailable", "This turn has ended.", request_id=request.id
                )
            identifier = _required_str(request.params, "request_id", request_id=request.id)
            try:
                accepted = (
                    broker.touch(identifier)
                    if request.method.endswith(".touch")
                    else broker.submit(identifier, request.params.get("answers"))
                )
            except (ValueError, TypeError) as exc:
                raise ProtocolError("invalid_answer", str(exc), request_id=request.id) from exc
            if not accepted:
                raise ProtocolError(
                    "question_expired", "This question has already ended.", request_id=request.id
                )
        return {
            "session_id": session.session_id,
            "job_id": job_id,
            "questions": broker.snapshots(finished=not _job_is_active(job)),
        }

    def _session_questions_create(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        questions = request.params.get("questions")
        if not isinstance(questions, list):
            raise ProtocolError(
                "invalid_field", "questions must be an array.", request_id=request.id
            )
        idempotency_key = _required_str(request.params, "idempotency_key", request_id=request.id)
        expires_in = request.params.get("expires_in_seconds", 3600.0)
        try:
            result = self._structured_state_for_session(
                session, request_id=request.id
            ).create_question_set(
                session_id=session.session_id,
                idempotency_key=idempotency_key,
                questions=questions,
                expires_in_seconds=expires_in,
            )
        except QuestionIdempotencyConflict as exc:
            raise ProtocolError(
                "question_idempotency_conflict", str(exc), request_id=request.id
            ) from exc
        except StructuredStateError as exc:
            raise ProtocolError("structured_state_error", str(exc), request_id=request.id) from exc
        payload = result.question_set.public_payload()
        if result.created:
            session.surface.emit_activity(
                ActivityEvent(
                    activity_id=f"questions-{result.question_set.question_set_id}",
                    kind="plan",
                    operation="ask_structured_question",
                    display_title="Waiting for your decision",
                    status="blocked",
                    summary=f"Alysis Code needs {len(result.question_set.questions)} decision(s) to continue.",
                )
            )
        return {"session_id": session.session_id, "created": result.created, **payload}

    def _session_questions_get(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        question_set_id = _required_str(request.params, "question_set_id", request_id=request.id)
        try:
            item = self._structured_state_for_session(
                session, request_id=request.id
            ).get_question_set(
                session_id=session.session_id,
                question_set_id=question_set_id,
            )
        except StructuredStateError as exc:
            raise ProtocolError("structured_state_error", str(exc), request_id=request.id) from exc
        if item is None:
            raise ProtocolError(
                "question_not_found", "Question set was not found.", request_id=request.id
            )
        return {"session_id": session.session_id, **item.public_payload()}

    def _session_questions_list(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        statuses = request.params.get("statuses")
        if statuses is not None and not isinstance(statuses, list):
            raise ProtocolError(
                "invalid_field", "statuses must be an array.", request_id=request.id
            )
        limit = _positive_int_param(
            request.params, "limit", default=50, upper=200, request_id=request.id
        )
        try:
            items = self._structured_state_for_session(
                session, request_id=request.id
            ).list_question_sets(
                session_id=session.session_id,
                statuses=statuses,
                limit=limit,
            )
        except StructuredStateError as exc:
            raise ProtocolError("structured_state_error", str(exc), request_id=request.id) from exc
        return {
            "session_id": session.session_id,
            "question_sets": [item.public_payload() for item in items],
            "count": len(items),
        }

    def _session_questions_answer(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        question_set_id = _required_str(request.params, "question_set_id", request_id=request.id)
        answers = request.params.get("answers")
        if not isinstance(answers, dict):
            raise ProtocolError(
                "invalid_field", "answers must be an object.", request_id=request.id
            )
        state = self._structured_state_for_session(session, request_id=request.id)
        try:
            lease = state.claim_question_set(
                session_id=session.session_id,
                question_set_id=question_set_id,
                resolver_id=f"bridge:{request.id}",
                lease_seconds=30.0,
            )
            if lease is None:
                raise ProtocolError(
                    "question_busy",
                    "Question set is being resolved by another client.",
                    request_id=request.id,
                )
            try:
                answered = state.answer_question_set(
                    session_id=session.session_id,
                    question_set_id=question_set_id,
                    lease_token=lease.lease_token,
                    answers=answers,
                )
            except Exception:
                with suppress(StructuredStateError):
                    state.release_question_set(
                        session_id=session.session_id,
                        question_set_id=question_set_id,
                        lease_token=lease.lease_token,
                    )
                raise
        except ProtocolError:
            raise
        except QuestionNotFound as exc:
            raise ProtocolError("question_not_found", str(exc), request_id=request.id) from exc
        except QuestionStateError as exc:
            raise ProtocolError("question_state_error", str(exc), request_id=request.id) from exc
        except StructuredStateError as exc:
            raise ProtocolError("structured_state_error", str(exc), request_id=request.id) from exc
        session.surface.emit_activity(
            ActivityEvent(
                activity_id=f"questions-{question_set_id}",
                kind="plan",
                operation="answer_structured_question",
                display_title="Decision received",
                status="succeeded",
            )
        )
        return {"session_id": session.session_id, **answered.public_payload()}

    def _session_questions_cancel(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        question_set_id = _required_str(request.params, "question_set_id", request_id=request.id)
        try:
            cancelled = self._structured_state_for_session(
                session, request_id=request.id
            ).cancel_question_set(
                session_id=session.session_id,
                question_set_id=question_set_id,
            )
        except QuestionNotFound as exc:
            raise ProtocolError("question_not_found", str(exc), request_id=request.id) from exc
        except QuestionStateError as exc:
            raise ProtocolError("question_state_error", str(exc), request_id=request.id) from exc
        except StructuredStateError as exc:
            raise ProtocolError("structured_state_error", str(exc), request_id=request.id) from exc
        return {"session_id": session.session_id, **cancelled.public_payload()}

    def _structured_state_for_session(
        self, session: BridgeSession, *, request_id: RequestId
    ) -> DurableStructuredState:
        if session.structured_state is None:
            try:
                session.structured_state = self._structured_state_factory(
                    owner_id="alysis-ide-bridge-v1",
                    workspace_root=session.root,
                )
            except StructuredStateError as exc:
                raise ProtocolError(
                    "structured_state_unavailable", str(exc), request_id=request_id
                ) from exc
        return session.structured_state

    def _resumable_swarm_for_session(
        self, session: BridgeSession, *, request_id: RequestId
    ) -> DurableResumableSwarmCoordinator:
        if session.resumable_swarm is None:
            try:
                session.resumable_swarm = self._resumable_swarm_factory(
                    owner_id="alysis-ide-bridge-v1",
                    workspace_root=session.root,
                )
                session.resumable_swarm.recover_stale_jobs(session_id=session.session_id)
            except ResumableSwarmError as exc:
                raise ProtocolError(
                    "resumable_swarm_unavailable", str(exc), request_id=request_id
                ) from exc
        return session.resumable_swarm

    def _managed_browser_for_session(
        self, session: BridgeSession, *, request_id: RequestId
    ) -> ManagedBrowserService:
        if session.managed_browser is None:
            try:
                session.managed_browser = self._managed_browser_factory(workspace_root=session.root)
            except BrowserError as exc:
                raise ProtocolError(
                    "managed_browser_unavailable", str(exc), request_id=request_id
                ) from exc
        return session.managed_browser

    def _checkpoint_mutation_session(self, request: ProtocolRequest) -> BridgeSession:
        self._require_workspace_trusted(request)
        self._require_confirmation(request)
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        with self._state_lock:
            _reconcile_session_job_state(session)
            if _job_is_active(session.active_job):
                raise ProtocolError(
                    "session_busy",
                    "Checkpoint changes require an idle session.",
                    request_id=request.id,
                )
        return session

    def _change_ledger_for_session(self, session: BridgeSession) -> ChangeLedger:
        if session.change_ledger is None:
            session.change_ledger = ChangeLedger(session.root)
        return session.change_ledger

    def _run_start(self, request: ProtocolRequest) -> tuple[dict[str, Any], Callable[[], None]]:
        params = dict(request.params)
        _reject_unsupported_run_start_fields(params, request_id=request.id)
        if "message" not in params and "instruction" in params:
            params["message"] = params["instruction"]
        explicit_session_id = "session_id" in params
        created_session: BridgeSession | None = None
        run_start_fingerprint: str | None = None
        if explicit_session_id:
            session_options = sorted(set(params) & (RUN_START_SESSION_FIELDS - {"session_id"}))
            if session_options:
                raise ProtocolError(
                    "unsupported_turn_option",
                    "run.start with an existing session only accepts turn params; use session.setMode, session.setModel, session.setStream, or session.setActiveWorkdir for live session changes.",
                    request_id=request.id,
                )
        else:
            raw_idempotency_key = params.get("idempotency_key")
            if raw_idempotency_key is not None and not isinstance(raw_idempotency_key, str):
                raise ProtocolError(
                    "invalid_field", "idempotency_key must be a string.", request_id=request.id
                )
            idempotency_key = str(raw_idempotency_key or uuid.uuid4().hex).strip()
            params["idempotency_key"] = idempotency_key
            # A sensitive host-context request must not run workspace subprocess
            # probes; session.create applies the same restriction to its binding.
            binding = _resolve_workspace(
                Path(str(params.get("workspace") or ".")),
                request_id=request.id,
                allow_subprocess=params.get("sensitive_media") is not True,
            )
            workspace_root = binding.workspace_context.workspace_root
            session_id = _run_start_session_id(workspace_root, idempotency_key)
            run_start_fingerprint = _run_start_request_fingerprint(
                workspace_root=workspace_root,
                params=params,
            )
            create_params = {
                key: value for key, value in params.items() if key in RUN_START_SESSION_FIELDS
            }
            with self._state_lock:
                existing = self._sessions.get(session_id)
                if existing is not None and existing.closed:
                    self._remove_session_locked(session_id)
                    existing = None
            if existing is None:
                create_params["session_id"] = session_id
                create_request = ProtocolRequest(
                    id=request.id,
                    method="session.create",
                    params=create_params,
                    protocol_version=request.protocol_version,
                )
                created = self._session_create(create_request)
                params["session_id"] = created["session_id"]
                with self._state_lock:
                    created_session = self._sessions.get(created["session_id"])
            else:
                params["session_id"] = existing.session_id
        turn_params = {key: value for key, value in params.items() if key in RUN_START_TURN_FIELDS}
        turn_params["session_id"] = params["session_id"]
        chat_request = ProtocolRequest(
            id=request.id,
            method="chat.send",
            params=turn_params,
            protocol_version=request.protocol_version,
        )
        try:
            return self._chat_send(
                chat_request,
                run_start_fingerprint=run_start_fingerprint,
            )
        except Exception:
            if created_session is not None:
                self._close_session(created_session)
                with self._state_lock:
                    self._sessions.pop(created_session.session_id, None)
                    self._event_buffers.pop(created_session.session_id, None)
                    self._event_dropped_counts.pop(created_session.session_id, None)
                    self._trace_view_floors.pop(created_session.session_id, None)
            raise

    def _schedule_prompt_reconciliation(
        self,
        session: BridgeSession,
        job: BridgeJob,
    ) -> None:
        """Observe a replayed live lease without ever executing it twice.

        A replacement bridge can receive the same durable ``run.start`` while
        the previous bridge's lease is still live. Polling the queue lets this
        bridge mirror a terminal result or reclaim an expired lease. The queue
        lease and durable turn-start marker remain the execution fence.
        """

        with self._state_lock:
            if job.prompt_reconciliation_started:
                return
            job.prompt_reconciliation_started = True

        def _finish_reconciliation() -> None:
            with self._state_lock:
                job.prompt_reconciliation_started = False

        def _reconcile() -> None:
            if self._closed or session.closed:
                _finish_reconciliation()
                return
            try:
                item = self._prompt_queue.get(
                    session_id=session.session_id,
                    prompt_id=job.prompt_id or job.job_id,
                )
            except PromptQueueError:
                _finish_reconciliation()
                return
            if item is None:
                _finish_reconciliation()
                return
            if item.state in {
                PromptState.COMPLETED,
                PromptState.CANCELLED,
                PromptState.FAILED,
            }:
                with self._state_lock:
                    status = item.state.value
                    _mark_job_completed(
                        job,
                        status=status,
                        exit_code=0 if item.state is PromptState.COMPLETED else 1,
                        error=item.error_code,
                    )
                    _reconcile_session_job_state(session)
                    job.prompt_reconciliation_started = False
                self._drain_prompt_queue(session)
                return
            try:
                reclaimable = self._prompt_queue.is_reclaimable(
                    session_id=session.session_id,
                    prompt_id=item.prompt_id,
                )
            except PromptQueueError:
                _finish_reconciliation()
                return
            if reclaimable:
                with self._state_lock:
                    if session.active_job is job:
                        session.active_job = None
                    job.status = "queued"
                    job.prompt_reconciliation_started = False
                self._drain_prompt_queue(session)
                return
            timer = threading.Timer(1.0, _reconcile)
            timer.daemon = True
            timer.start()

        _reconcile()

    def _resume_prompt_queue(self, session: BridgeSession) -> None:
        """Attach a replacement session to a live inherited queue lease."""

        try:
            running = self._prompt_queue.list(
                session_id=session.session_id,
                states=[PromptState.RUNNING],
                limit=2,
            )
        except PromptQueueError as exc:
            session.surface.emit_error("prompt_queue_recovery_failed", str(exc), False)
            return
        if not running:
            self._drain_prompt_queue(session)
            return
        item = running[0]
        with self._state_lock:
            job = self._jobs.get(item.prompt_id)
            if job is None:
                self._prune_terminal_jobs_locked(reserve=1)
                job = BridgeJob(
                    job_id=item.prompt_id,
                    session_id=session.session_id,
                    created_at=_timestamp_iso(item.created_at),
                    kind="session_turn",
                )
                self._jobs[job.job_id] = job
            job.status = "running"
            job.prompt_id = item.prompt_id
            job.prompt_sequence = item.sequence
            job.prompt_lease_expires_at = item.lease_expires_at
            session.active_job = job
        self._schedule_prompt_reconciliation(session, job)

    def _drain_prompt_queue(self, session: BridgeSession) -> None:
        while True:
            with self._state_lock:
                _reconcile_session_job_state(session)
                if (
                    self._closed
                    or session.closed
                    or session.close_when_idle
                    or _job_is_active(session.active_job)
                ):
                    return
                try:
                    lease = self._prompt_queue.claim_next(
                        session_id=session.session_id,
                        owner_id=self._prompt_queue_owner_id,
                        lease_seconds=DEFAULT_LEASE_SECONDS,
                    )
                except PromptQueueError as exc:
                    session.surface.emit_error("prompt_queue_claim_failed", str(exc), False)
                    return
                if lease is None:
                    return
                item = lease.item
                job = self._jobs.get(item.prompt_id)
                if job is None:
                    self._prune_terminal_jobs_locked(reserve=1)
                    job = BridgeJob(
                        job_id=item.prompt_id,
                        session_id=session.session_id,
                        created_at=_timestamp_iso(item.created_at),
                        kind="session_turn",
                    )
                    self._jobs[job.job_id] = job
                job.status = "queued"
                job.prompt_id = item.prompt_id
                job.prompt_sequence = item.sequence
                job.prompt_lease_token = lease.lease_token
                job.prompt_lease_expires_at = lease.expires_at
                session.active_job = job

            payload = item.payload
            message = payload.get("message")
            image_paths = payload.get("image_paths", [])
            basket_image_paths = payload.get("basket_image_paths", [])
            context_prompt = payload.get("context_prompt", "")
            if (
                not isinstance(message, str)
                or not isinstance(context_prompt, str)
                or not _is_string_list(image_paths)
                or not _is_string_list(basket_image_paths)
            ):
                with suppress(PromptQueueError):
                    self._prompt_queue.fail(
                        session_id=session.session_id,
                        prompt_id=item.prompt_id,
                        lease_token=lease.lease_token,
                        error_code="invalid_payload",
                    )
                with self._state_lock:
                    _mark_job_completed(
                        job, status="failed", exit_code=1, error="invalid_queue_payload"
                    )
                    _reconcile_session_job_state(session)
                session.surface.emit_error(
                    "invalid_queue_payload", "Queued prompt data was invalid.", False
                )
                continue

            if _prompt_completed_in_store(session, item.prompt_id):
                try:
                    self._prompt_queue.complete(
                        session_id=session.session_id,
                        prompt_id=item.prompt_id,
                        lease_token=lease.lease_token,
                    )
                except PromptQueueError as exc:
                    session.surface.emit_error("prompt_queue_reconcile_failed", str(exc), False)
                    return
                with self._state_lock:
                    _mark_job_completed(job, status="completed", exit_code=0)
                    _reconcile_session_job_state(session)
                session.surface.emit_activity(
                    ActivityEvent(
                        activity_id=f"prompt-{item.prompt_id}",
                        kind="plan",
                        operation="reconcile_prompt",
                        display_title="Recovered completed work",
                        status="succeeded",
                    )
                )
                continue

            if item.execution_started_at is not None or _prompt_started_in_store(
                session, item.prompt_id
            ):
                # A prior process durably crossed the execution boundary but
                # never recorded completion. Retrying automatically could
                # duplicate shell/network side effects, so fail closed and let
                # the user explicitly retry with a fresh prompt.
                with suppress(PromptQueueError):
                    self._prompt_queue.fail(
                        session_id=session.session_id,
                        prompt_id=item.prompt_id,
                        lease_token=lease.lease_token,
                        error_code="interrupted_indeterminate",
                    )
                with self._state_lock:
                    _mark_job_completed(
                        job,
                        status="failed",
                        exit_code=1,
                        error="interrupted_indeterminate",
                    )
                    _reconcile_session_job_state(session)
                session.surface.emit_activity(
                    ActivityEvent(
                        activity_id=f"prompt-{item.prompt_id}",
                        kind="plan",
                        operation="recover_prompt",
                        display_title="Interrupted work needs review",
                        status="failed",
                        summary="Automatic retry was stopped to avoid duplicate side effects.",
                    )
                )
                session.surface.emit_warning(
                    f"prompt_recovery_stopped {item.prompt_id} reason=interrupted_indeterminate"
                )
                continue

            thread = threading.Thread(
                target=self._run_session_turn,
                args=(
                    session,
                    job,
                    message,
                    list(image_paths),
                    list(basket_image_paths),
                    context_prompt,
                    lease,
                ),
                kwargs={"skill": payload.get("skill")},
                name=f"alysis-ide-{job.job_id}",
                daemon=True,
            )
            if self._start_job_thread(session, job, thread):
                return
            with suppress(PromptQueueError):
                self._prompt_queue.fail(
                    session_id=session.session_id,
                    prompt_id=item.prompt_id,
                    lease_token=lease.lease_token,
                    error_code="job_start_failed",
                )
            # The failed claim is terminal, so keep draining later prompts rather
            # than leaving the session idle until the original lease expires.
            continue

    def _run_session_turn(
        self,
        session: BridgeSession,
        job: BridgeJob,
        message: str,
        image_paths: list[str],
        basket_image_paths: list[str],
        context_prompt: str = "",
        prompt_lease: PromptLease | None = None,
        *,
        skill: dict[str, str] | None = None,
    ) -> None:
        session.surface.with_job(job.job_id)
        host_image_binding = job.host_image_binding
        heartbeat_stop = threading.Event()
        heartbeat_lost = threading.Event()
        heartbeat: threading.Thread | None = None
        # A sensitive host-context session is answer-only and never logs, so it
        # owns neither durable prompt markers nor a workspace change ledger.
        checkpoint_supported = not session.sensitive_media and _checkpointing_supported(session)
        turn_touched_paths: set[str] = set()
        previous_outcome = getattr(session.agent_session, "last_turn_outcome", None)

        def turn_outcome_fields(exit_code: int, reason: str = "completed") -> dict[str, Any]:
            current = getattr(session.agent_session, "last_turn_outcome", None)
            # Rejected input and failures before run_turn must not reuse a
            # prior accepted task's verdict or attribute its identity here.
            fields = task_outcome_fields(
                None if current is previous_outcome else current,
                exit_code=exit_code,
                reason=reason,
            )
            outcome = fields["task_outcome"]
            if reason == "cancelled":
                outcome.update(
                    outcome="cancelled", verified_success=False, reason=reason, exit_code=exit_code
                )
            elif exit_code != 0 and outcome["outcome"] in {
                "verified_success",
                "completed_unverified",
            }:
                outcome.update(
                    outcome="incomplete", verified_success=False, reason=reason, exit_code=exit_code
                )
            fields["verified_success"] = outcome["verified_success"]
            return fields

        try:
            if prompt_lease is not None:
                self._prompt_queue.mark_execution_started(
                    session_id=session.session_id,
                    prompt_id=prompt_lease.item.prompt_id,
                    lease_token=prompt_lease.lease_token,
                )
                heartbeat = threading.Thread(
                    target=self._prompt_lease_heartbeat,
                    args=(session, job, prompt_lease, heartbeat_stop, heartbeat_lost),
                    name=f"alysis-lease-{job.job_id}",
                    daemon=True,
                )
                heartbeat.start()
            worker_image_context = (
                use_ide_host_image_binding(host_image_binding, prepared=job.prepared_host_image)
                if host_image_binding is not None
                else nullcontext()
            )
            budget_context = self._task_budget_context(session, job)
            with worker_image_context, budget_context:
                if session.sensitive_media:
                    _assert_live_computer_use_destination(session, request_id=None)
                with self._state_lock:
                    _mark_job_running(job)
                started_recorded = _append_prompt_store_event(
                    session, "ide_prompt_started", job.prompt_id
                )
                if checkpoint_supported and not started_recorded:
                    raise RuntimeError("Could not persist the prompt execution marker.")
                session.surface.emit_info(f"job_started {job.job_id}")
                session.surface.emit_activity(
                    ActivityEvent(
                        activity_id=f"prompt-{job.prompt_id or job.job_id}",
                        kind="plan",
                        operation="execute_prompt",
                        display_title="Working on request",
                        status="running",
                    )
                )
                if checkpoint_supported and session.checkpoint_guard is None:
                    # Alternate session implementations without a dispatch hook still
                    # need an eager baseline. Production sessions prepare it lazily,
                    # before the first tool; text-only replies never need Git storage.
                    _prepare_change_ledger_with_deadline(session)
                _check_job_cancelled(job)
                cumulative_touched = getattr(
                    session.agent_session, "workspace_touched_paths", set()
                )
                tracker = _TurnTouchedPathSet(
                    cumulative_touched if isinstance(cumulative_touched, set) else ()
                )
                session.agent_session.workspace_touched_paths = tracker
                turn_touched_paths = tracker.recorded
                # Captured files, diagnostics and terminal output are context,
                # never the accepted instruction or its durable task identity.
                # Keep their untrusted wrapper in the existing per-turn channel.
                context_messages = [context_prompt] if context_prompt.strip() else []
                if skill is not None:
                    # Resolve against this session's effective, plugin-filtered registry again
                    # after queue recovery. Instructions never become permanent chat messages.
                    context_messages.append(skill_context_message(session.agent_session, skill))
                    session.surface.emit_activity(
                        ActivityEvent(
                            activity_id=f"skill-{job.prompt_id or job.job_id}",
                            kind="read",
                            operation="skill_invoke",
                            display_title=f"Using {skill['name']}",
                            status="succeeded",
                        )
                    )
                if session.sensitive_media:
                    _assert_live_computer_use_destination(session, request_id=None)
                if image_paths:
                    exit_code = int(
                        _run_agent_turn_with_optional_cancellation(
                            session.agent_session,
                            message,
                            image_paths=image_paths,
                            ephemeral_user_messages=context_messages,
                            cancellation_token=BridgeCancellationToken(job.cancellation_event),
                        )
                        or 0
                    )
                else:
                    exit_code = int(
                        _run_agent_turn_with_optional_cancellation(
                            session.agent_session,
                            message,
                            ephemeral_user_messages=context_messages,
                            cancellation_token=BridgeCancellationToken(job.cancellation_event),
                        )
                        or 0
                    )
            if heartbeat_lost.is_set():
                raise PromptLeaseLost("Prompt lease is no longer valid.")
            with self._state_lock:
                # Serialize the final cancellation checkpoint with session.cancel's
                # transition.  Otherwise Stop can set cancellation_requested after
                # the checkpoint but before this completion write, allowing a late
                # success to overwrite the accepted cancellation request.
                _check_job_cancelled(job)
                outcome_fields = turn_outcome_fields(exit_code)
                if not checkpoint_supported:
                    # Lightweight protocol test/fallback sessions (and sensitive
                    # host-context sessions) do not own a durable SessionStore.
                    # Preserve the historical observable completion timing while
                    # the queue transaction settles immediately afterward.
                    _mark_job_completed(
                        job, status="completed", exit_code=exit_code, result=outcome_fields
                    )
            checkpoint = (
                session.change_ledger.capture(
                    session.session_id,
                    turn_id=job.job_id,
                    kind="turn",
                    message="IDE chat turn",
                    paths=turn_touched_paths,
                )
                if session.change_ledger is not None and turn_touched_paths
                else None
            )
            completed_recorded = _append_prompt_store_event(
                session,
                "ide_prompt_completed",
                job.prompt_id,
                checkpoint_id=checkpoint.checkpoint_id if checkpoint is not None else None,
            )
            if checkpoint_supported and not completed_recorded:
                raise RuntimeError("Could not persist the prompt completion marker.")
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=1.0)
            if prompt_lease is not None:
                self._prompt_queue.complete(
                    session_id=session.session_id,
                    prompt_id=prompt_lease.item.prompt_id,
                    lease_token=prompt_lease.lease_token,
                )
            with self._state_lock:
                _mark_job_completed(
                    job,
                    status="completed",
                    exit_code=exit_code,
                    result={
                        **outcome_fields,
                        "checkpoint_id": (
                            checkpoint.checkpoint_id if checkpoint is not None else None
                        ),
                        "changed_files": (
                            [record.path for record in checkpoint.changes]
                            if checkpoint is not None
                            else []
                        ),
                        "omitted_paths": (
                            list(checkpoint.omitted_paths) if checkpoint is not None else []
                        ),
                        "non_revertible_paths": (
                            list(checkpoint.non_revertible_paths) if checkpoint is not None else []
                        ),
                        "fully_revertible": bool(
                            checkpoint is not None and not checkpoint.non_revertible_paths
                        ),
                    },
                )
                if basket_image_paths:
                    _remove_pending_images(session, basket_image_paths)
                _reconcile_session_job_state(session)
            session.surface.emit_activity(
                ActivityEvent(
                    activity_id=f"prompt-{job.prompt_id or job.job_id}",
                    kind="plan",
                    operation="execute_prompt",
                    display_title=(
                        "Request verified"
                        if outcome_fields["verified_success"]
                        else "Request finished · unverified"
                    ),
                    status=(
                        "succeeded"
                        if outcome_fields["verified_success"]
                        else "completed_unverified"
                    ),
                    metadata={
                        "task_outcome": outcome_fields["task_outcome"]["outcome"],
                        "verified_success": outcome_fields["verified_success"],
                    },
                    summary=(
                        f"Checkpoint {checkpoint.checkpoint_id[:8]} recorded."
                        if checkpoint is not None
                        else None
                    ),
                    files=(
                        tuple(record.path for record in checkpoint.changes)
                        if checkpoint is not None
                        else ()
                    ),
                )
            )
            if job.browser_start_handoff is not None and exit_code == 0:
                # Private handoff authority is never placed in replay, trace or session history.
                self._write_transient_computer_use_request(
                    session.surface.transient_envelope(
                        ProtocolPayloadEvent(
                            "info_emitted",
                            {
                                "message": f"job_completed {job.job_id} exit_code=0",
                                "browser_start_handoff": dict(job.browser_start_handoff),
                            },
                        )
                    )
                )
            else:
                session.surface.emit_info(f"job_{job.status} {job.job_id} exit_code={exit_code}")
        except BridgeCancellationError as e:
            error_message_text = _redact_bridge_error(
                session,
                e.reason or "cancelled_by_user",
                host_image_binding=host_image_binding,
                image_paths=image_paths,
            )
            self._capture_failed_turn_checkpoint(session, job, paths=turn_touched_paths)
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=1.0)
            if prompt_lease is not None:
                with suppress(PromptQueueError):
                    self._prompt_queue.cancel_claimed(
                        session_id=session.session_id,
                        prompt_id=prompt_lease.item.prompt_id,
                        lease_token=prompt_lease.lease_token,
                    )
            with self._state_lock:
                _mark_job_completed(
                    job,
                    status="cancelled",
                    exit_code=130,
                    error=error_message_text,
                    result=turn_outcome_fields(130, "cancelled"),
                )
                _reconcile_session_job_state(session)
            session.surface.emit_warning(f"job_cancelled {job.job_id} reason={error_message_text}")
        except Exception as e:  # noqa: BLE001
            self._capture_failed_turn_checkpoint(session, job, paths=turn_touched_paths)
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=1.0)
            with self._state_lock:
                # A failure observed after an accepted Stop is reported as that
                # cancellation. A lost prompt lease also sets the cancellation
                # event, but it remains an execution failure.
                cancelled = (
                    job.cancellation_event.is_set()
                    and not heartbeat_lost.is_set()
                    and not isinstance(e, PromptLeaseLost)
                )
                safe_error = _redact_bridge_error(
                    session,
                    (job.cancellation_reason or "cancelled_by_user") if cancelled else e,
                    host_image_binding=host_image_binding,
                    image_paths=image_paths,
                )
            if prompt_lease is not None:
                with suppress(PromptQueueError):
                    if cancelled:
                        self._prompt_queue.cancel_claimed(
                            session_id=session.session_id,
                            prompt_id=prompt_lease.item.prompt_id,
                            lease_token=prompt_lease.lease_token,
                        )
                    else:
                        self._prompt_queue.fail(
                            session_id=session.session_id,
                            prompt_id=prompt_lease.item.prompt_id,
                            lease_token=prompt_lease.lease_token,
                            error_code=(
                                "lease_lost"
                                if isinstance(e, PromptLeaseLost)
                                else "execution_failed"
                            ),
                        )
            with self._state_lock:
                _mark_job_completed(
                    job,
                    status="cancelled" if cancelled else "failed",
                    exit_code=130 if cancelled else 1,
                    error=safe_error,
                    result=turn_outcome_fields(
                        130 if cancelled else 1, "cancelled" if cancelled else "terminal_error"
                    ),
                )
                _reconcile_session_job_state(session)
            if cancelled:
                session.surface.emit_warning(f"job_cancelled {job.job_id} reason={safe_error}")
            else:
                session.surface.emit_error("job_failed", safe_error, False)
        finally:
            heartbeat_stop.set()
            with self._state_lock:
                # A completed/cancelled/failed job can be retained in history;
                # the raw one-shot descriptor must never be retained with it.
                job.host_image_binding = None
                job.prepared_host_image = None
                job.browser_start_handoff = None
                with session.approval_lock:
                    session.approved_approval_scopes[:] = [
                        scope
                        for scope in session.approved_approval_scopes
                        if scope.kind != "computer_use"
                    ]
                _reconcile_session_job_state(session)
                self._prune_terminal_jobs_locked()
                # Steering this turn did not read must not reach the next turn.
                _hold_unread_steers(session)
            if session.task_budget_authority is not None:
                session.task_budget_authority.close()
            if session.task_budget_channel is not None:
                session.task_budget_channel.close()
            if host_image_binding is not None:
                _clear_bridge_host_screen_redactor(session)
            session.surface.with_job(None)
            self._drain_prompt_queue(session)

    def _prompt_lease_heartbeat(
        self,
        session: BridgeSession,
        job: BridgeJob,
        lease: PromptLease,
        stop: threading.Event,
        lost: threading.Event,
    ) -> None:
        interval = max(1.0, min(30.0, DEFAULT_LEASE_SECONDS / 3.0))
        while not stop.wait(interval):
            try:
                renewed = self._prompt_queue.renew(
                    session_id=session.session_id,
                    prompt_id=lease.item.prompt_id,
                    lease_token=lease.lease_token,
                    lease_seconds=DEFAULT_LEASE_SECONDS,
                )
            except PromptQueueError:
                lost.set()
                job.cancellation_event.set()
                return
            job.prompt_lease_expires_at = renewed.expires_at

    def _capture_failed_turn_checkpoint(
        self,
        session: BridgeSession,
        job: BridgeJob,
        *,
        paths: Iterable[str],
    ) -> None:
        if session.change_ledger is None or not paths:
            return
        try:
            session.change_ledger.capture(
                session.session_id,
                turn_id=job.job_id,
                kind="turn",
                message="IDE chat turn (failed)",
                paths=paths,
            )
        except ChangeLedgerError as exc:
            session.surface.emit_error("checkpoint_failed", str(exc), False)

    def _start_job_thread(
        self,
        session: BridgeSession,
        job: BridgeJob,
        thread: threading.Thread,
    ) -> bool:
        """Start an acknowledged background job without leaving it queued forever on OS failure."""
        message: str | None = None
        with self._state_lock:
            job.thread = thread
            try:
                # Serialize the transition from queued -> started with bridge
                # shutdown.  The worker may block briefly on this same lock,
                # but Thread.start() itself does not wait for the target.
                thread.start()
            except Exception as exc:  # noqa: BLE001 - OS resource pressure.
                message = _redact_bridge_error(
                    session,
                    f"job_start_failed: {exc}",
                    host_image_binding=job.host_image_binding,
                )
                _mark_job_completed(job, status="failed", exit_code=1, error=message)
                job.host_image_binding = None
                job.prepared_host_image = None
                _reconcile_session_job_state(session)
                self._prune_terminal_jobs_locked()
        if message is not None:
            session.surface.emit_error("job_start_failed", message, False)
            _clear_bridge_host_screen_redactor(session)
            return False
        return True

    def _session_status(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        with self._state_lock:
            _reconcile_session_job_state(session)
        return _session_status_payload(session)

    def _session_usage(self, request: ProtocolRequest) -> dict[str, Any]:
        session_id = _required_str(request.params, "session_id", request_id=request.id)
        with self._state_lock:
            session = self._sessions.get(session_id)
        if session is None or session.closed:
            return retained_session_usage(request.params, request_id=request.id)
        usage = getattr(session.agent_session, "usage_summary", None)
        by_model = usage.by_model_rows() if hasattr(usage, "by_model_rows") else []
        totals = usage.totals() if hasattr(usage, "totals") else {"calls": 0}
        records = usage.records() if hasattr(usage, "records") else []
        return {
            "session_id": session.session_id,
            "by_model": by_model,
            "totals": totals,
            "call_count": int(totals.get("calls", len(records)) if isinstance(totals, dict) else 0),
        }

    def _session_history(self, request: ProtocolRequest) -> dict[str, Any]:
        params = request.params
        session = self._require_session(
            _required_str(params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        pattern = _required_str(params, "pattern", request_id=request.id)
        max_results = _positive_int_param(
            params,
            "max_results",
            default=50,
            upper=500,
            request_id=request.id,
        )
        max_text_chars = _positive_int_param(
            params,
            "max_text_chars",
            default=500,
            upper=4000,
            request_id=request.id,
        )
        matches: list[dict[str, Any]] = []
        lowered = pattern.casefold()
        messages = getattr(session.agent_session, "messages", [])
        scanned_count = 0
        truncated = False
        if isinstance(messages, list):
            for index, message in enumerate(messages, start=1):
                scanned_count += 1
                content = _message_content_text(message)
                if lowered in content.casefold():
                    redacted_text = str(redact_secrets(content))
                    matches.append(
                        {
                            "kind": "message",
                            "path": f"session:{session.session_id}",
                            "line": index,
                            "text": redacted_text[:max_text_chars],
                            "text_truncated": len(redacted_text) > max_text_chars,
                        }
                    )
                if len(matches) >= max_results:
                    truncated = index < len(messages)
                    break
        if len(matches) < max_results:
            store = getattr(session.agent_session, "store", None)
            artifact_root = getattr(store, "session_artifact_root", None)
            try:
                artifact_result = history_search(
                    root=session.root,
                    session_id=session.session_id,
                    session_artifact_root=(
                        Path(artifact_root) if artifact_root is not None else None
                    ),
                    pattern=re.escape(pattern),
                    max_results=max_results - len(matches),
                    max_file_bytes=200_000,
                    max_total_bytes=2 * 1024 * 1024,
                    max_snippet_chars=max_text_chars,
                    include_history=True,
                    include_tool_outputs=True,
                    include_memory=True,
                )
            except HistorySearchError:
                artifact_result = {"matches": [], "truncated": False, "scanned_files": 0}
            matches.extend(list(artifact_result.get("matches") or []))
            scanned_count += int(artifact_result.get("scanned_files") or 0)
            truncated = truncated or bool(artifact_result.get("truncated"))
        return {
            "session_id": session.session_id,
            "pattern": pattern,
            "matches": matches,
            "redacted": True,
            "secret_values_included": False,
            "max_results": max_results,
            "max_text_chars": max_text_chars,
            "truncated": truncated,
            "scanned_count": scanned_count,
        }

    def _session_search(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        query = _required_str(request.params, "query", request_id=request.id)
        max_results = _positive_int_param(
            request.params,
            "max_results",
            default=25,
            upper=50,
            request_id=request.id,
        )
        max_sessions = _positive_int_param(
            request.params,
            "max_sessions",
            default=30,
            upper=50,
            request_id=request.id,
        )
        try:
            result = search_workspace_sessions(
                sessions_dir=_session_log_dir(session.agent_session),
                workspace_root=session.root,
                query=query,
                limits=SessionSearchLimits(
                    max_sessions=max_sessions,
                    max_results=max_results,
                ),
            )
        except SessionSearchError as exc:
            raise ProtocolError("invalid_session_search", str(exc), request_id=request.id) from exc
        for item in result["results"]:
            item["context_block"] = past_session_context_block(item)
        result["session_id"] = session.session_id
        return result

    def _session_context(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        cfg = getattr(session.agent_session, "cfg", None)
        model_name = str(getattr(cfg, "model", "") or "")
        messages = _session_messages(session.agent_session)
        message_count = len(messages)
        pinned_prefix_len = _pinned_prefix_len(session.agent_session, messages)
        context_left = _call_context_left(session.agent_session)
        breakdown = estimate_request_token_breakdown(
            messages=messages,
            tool_list=_session_tool_list(session.agent_session),
            pinned_prefix_len=pinned_prefix_len,
        ).to_payload()
        if context_left is not None:
            model_name = str(getattr(context_left, "model_name", "") or model_name)
            max_input_tokens = _non_negative_int_or_none(
                getattr(context_left, "max_input_tokens", None)
            )
            used_input_tokens = _non_negative_int_or_none(
                getattr(context_left, "used_input_tokens", None)
            )
            remaining_tokens = _non_negative_int_or_none(
                getattr(context_left, "remaining_tokens", None)
            )
            percent_left = _float_or_none(getattr(context_left, "percent_left", None))
            effective_input_budget = _non_negative_int_or_none(
                getattr(context_left, "effective_input_budget", None)
            )
            effective_remaining_tokens = _non_negative_int_or_none(
                getattr(context_left, "effective_remaining_tokens", None)
            )
            effective_percent_left = _float_or_none(
                getattr(context_left, "effective_percent_left", None)
            )
            token_usage_available = used_input_tokens is not None
            source = "tokenizer_estimate" if token_usage_available else "unavailable"
            return {
                "session_id": session.session_id,
                "model_name": model_name,
                "max_input_tokens": max_input_tokens,
                "used_input_tokens": used_input_tokens,
                "remaining_tokens": remaining_tokens,
                "percent_left": percent_left,
                "source": source,
                "provider_metadata_source": str(getattr(context_left, "source", "") or ""),
                "approximate": token_usage_available,
                "token_usage_available": token_usage_available,
                "message_count": message_count,
                "pinned_prefix_len": pinned_prefix_len,
                "token_breakdown": breakdown,
                "effective_input_budget": effective_input_budget,
                "effective_remaining_tokens": effective_remaining_tokens,
                "effective_percent_left": effective_percent_left,
                "context_window_tokens": _non_negative_int_or_none(
                    getattr(context_left, "context_window_tokens", None)
                ),
                "context_window_remaining_tokens": _non_negative_int_or_none(
                    getattr(context_left, "context_window_remaining_tokens", None)
                ),
                "context_window_percent_left": _float_or_none(
                    getattr(context_left, "context_window_percent_left", None)
                ),
                "startup_baseline_tokens": _non_negative_int_or_none(
                    getattr(context_left, "startup_baseline_tokens", None)
                ),
                "dynamic_context_budget_tokens": _non_negative_int_or_none(
                    getattr(context_left, "dynamic_context_budget_tokens", None)
                ),
                "dynamic_context_used_tokens": _non_negative_int_or_none(
                    getattr(context_left, "dynamic_context_used_tokens", None)
                ),
                "dynamic_context_remaining_tokens": _non_negative_int_or_none(
                    getattr(context_left, "dynamic_context_remaining_tokens", None)
                ),
                "dynamic_context_percent_left": _float_or_none(
                    getattr(context_left, "dynamic_context_percent_left", None)
                ),
            }
        used_input_tokens = int(breakdown["total_tokens"])
        return {
            "session_id": session.session_id,
            "model_name": model_name,
            "max_input_tokens": None,
            "used_input_tokens": used_input_tokens,
            "remaining_tokens": None,
            "percent_left": None,
            "source": "tokenizer_estimate",
            "provider_metadata_source": None,
            "approximate": True,
            "token_usage_available": True,
            "message_count": message_count,
            "pinned_prefix_len": pinned_prefix_len,
            "token_breakdown": breakdown,
            "effective_input_budget": None,
            "effective_remaining_tokens": None,
            "effective_percent_left": None,
        }

    def _session_compact(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        if _job_is_active(session.active_job):
            raise ProtocolError(
                "session_busy",
                "Wait for the current turn before compacting.",
                request_id=request.id,
            )
        focus = _optional_str(request.params, "focus", request_id=request.id)
        messages = _session_messages(session.agent_session)
        tool_list = _session_tool_list(session.agent_session)
        compactor = getattr(session.agent_session, "conversation_compactor", None)
        compact_fn = getattr(compactor, "compact_now", None)
        tokens_before = estimate_request_tokens(messages, tool_list)
        chunks_before = int(
            getattr(getattr(compactor, "state", None), "history_chunk_index", 0) or 0
        )
        pins_before = len(getattr(getattr(compactor, "state", None), "pins", []) or [])
        if compactor is None or not callable(compact_fn):
            return {
                "session_id": session.session_id,
                "supported": False,
                "changed": False,
                "focus": focus,
                "tokens_before": tokens_before,
                "tokens_after": tokens_before,
                "tokens_delta": 0,
                "message_count": len(messages),
                "source": "tokenizer_estimate",
                "approximate": True,
                "reason": "Conversation compaction is disabled or unavailable for this session.",
            }
        model = str(
            getattr(getattr(session.agent_session, "client", None), "model", "")
            or getattr(getattr(session.agent_session, "cfg", None), "model", "")
            or ""
        )
        before_fingerprint = _messages_fingerprint(messages)
        try:
            new_messages, changed = compact_fn(
                messages=messages,
                tool_list=tool_list,
                main_model=model,
                focus=focus,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProtocolError(
                "session_compaction_failed",
                str(redact_secrets(f"Session compaction failed: {exc}")),
                request_id=request.id,
            ) from exc
        if isinstance(new_messages, list):
            session.agent_session.messages = new_messages
        messages_after = _session_messages(session.agent_session)
        tokens_after = estimate_request_tokens(messages_after, tool_list)
        changed_bool = bool(changed) and _messages_fingerprint(messages_after) != before_fingerprint
        if changed_bool:
            store = getattr(session.agent_session, "store", None)
            if store is not None and callable(getattr(store, "append", None)):
                startup = getattr(session.agent_session, "startup_messages", [])
                store.append(
                    "conversation_summary_updated",
                    {
                        "active_conversation_messages": [
                            message for message in messages_after if message not in startup
                        ]
                    },
                )
        chunks_after = int(
            getattr(getattr(compactor, "state", None), "history_chunk_index", 0) or 0
        )
        pins_after = len(getattr(getattr(compactor, "state", None), "pins", []) or [])
        return {
            "session_id": session.session_id,
            "supported": True,
            "changed": changed_bool,
            "focus": focus,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "tokens_delta": tokens_after - tokens_before,
            "message_count": len(messages_after),
            "source": "tokenizer_estimate",
            "approximate": True,
            "chunks_before": chunks_before,
            "chunks_after": chunks_after,
            "pins_before": pins_before,
            "pins_after": pins_after,
        }

    def _session_fork(self, request: ProtocolRequest) -> dict[str, Any]:
        """Copy dialogue to a fresh sibling worktree, never approvals, tools or checkpoints."""
        import subprocess

        source = self._require_session(
            _required_str(request.params, "source_session_id", request_id=request.id),
            request_id=request.id,
        )
        target = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        if source is target or not source.workspace_trusted or not target.workspace_trusted:
            raise ProtocolError(
                "invalid_fork", "Fork requires two trusted sessions.", request_id=request.id
            )

        def common_dir(root: Path) -> Path:
            env = {
                key: value
                for key, value in os.environ.items()
                if key
                not in {
                    "GIT_DIR",
                    "GIT_COMMON_DIR",
                    "GIT_WORK_TREE",
                    "GIT_INDEX_FILE",
                    "GIT_OBJECT_DIRECTORY",
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                }
            }
            result = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=root,
                capture_output=True,
                **command_text(),
                timeout=10,
                check=True,
                env=env,
            )
            return (root / result.stdout.strip()).resolve(strict=True)

        try:
            related = common_dir(source.root) == common_dir(target.root)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProtocolError(
                "invalid_fork", "Cannot verify worktree ownership.", request_id=request.id
            ) from exc
        if not related or source.root == target.root:
            raise ProtocolError(
                "invalid_fork",
                "Sessions must belong to different worktrees of the same repository.",
                request_id=request.id,
            )
        with self._state_lock:
            for session in (source, target):
                _reconcile_session_job_state(session)
                if _job_is_active(session.active_job):
                    raise ProtocolError(
                        "session_busy",
                        "Finish or stop the active task before moving it.",
                        request_id=request.id,
                    )
            messages = getattr(target.agent_session, "messages", None)
            startup = getattr(target.agent_session, "startup_messages", None)
            fresh = (
                messages == startup
                if isinstance(startup, list)
                else not any(
                    m.get("role") in {"user", "assistant", "tool"}
                    for m in messages or []
                    if isinstance(m, dict)
                )
            )
            if (
                not isinstance(messages, list)
                or not fresh
                or target.last_job is not None
                or target.forked_from_session_id
            ):
                raise ProtocolError(
                    "invalid_fork",
                    "The destination must be a new conversation.",
                    request_id=request.id,
                )
            history = []
            size = 0
            source_messages = getattr(source.agent_session, "messages", [])
            source_startup = getattr(source.agent_session, "startup_messages", [])
            if source_startup and source_messages[: len(source_startup)] == source_startup:
                source_messages = source_messages[len(source_startup) :]
            for message in reversed(source_messages):
                if not isinstance(message, dict) or message.get("role") not in {
                    "user",
                    "assistant",
                }:
                    continue
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue
                content = str(redact_secrets(content))
                size += len(content.encode("utf-8"))
                if len(history) >= 200 or size > 256_000:
                    break
                history.append({"role": message["role"], "content": content})
            history.reverse()
            store = getattr(target.agent_session, "store", None)
            if store is None or not callable(getattr(store, "append", None)):
                raise ProtocolError(
                    "invalid_fork",
                    "Conversation persistence is unavailable.",
                    request_id=request.id,
                )
            for message in history:
                store.append(f"{message['role']}_message", {"content": message["content"]})
            messages.extend(history)
            target.forked_from_session_id = source.session_id
            for message in history:
                target.surface.emit_message_end(message["content"], role=message["role"])
            return {
                "session_id": target.session_id,
                "source_session_id": source.session_id,
                "history_count": len(history),
            }

    def _session_resume(self, request: ProtocolRequest) -> dict[str, Any]:
        params = request.params
        session = self._require_session(
            _required_str(params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        target = _required_str(params, "target_session_id", request_id=request.id)
        from ..cli_impl.commands.chat_resume_helpers import (
            _build_chat_resume_context_message,
            _insert_chat_resume_context_message,
            _is_chat_resume_context_message,
            _load_chat_resume_messages,
            _normalize_chat_resume_session_id,
            _resolve_chat_resume_session_path,
        )

        normalized_target = _normalize_chat_resume_session_id(target)
        if normalized_target is None:
            raise ProtocolError(
                "invalid_session_id",
                "target_session_id contains unsupported characters.",
                request_id=request.id,
            )
        with self._state_lock:
            active_source = self._sessions.get(normalized_target)
            if (
                normalized_target != session.session_id
                and active_source is not None
                and not active_source.closed
            ):
                raise ProtocolError(
                    "resume_session_active",
                    "The retained session is still active and cannot share a checkpoint lineage.",
                    request_id=request.id,
                )
        max_messages = _bounded_int(
            params.get("max_messages"),
            default=200,
            upper=500,
            request_id=request.id,
        )
        sessions_dir = _session_log_dir(session.agent_session)
        target_path = _resolve_chat_resume_session_path(
            sessions_dir=sessions_dir,
            session_id=normalized_target,
        )
        if target_path is None:
            raise ProtocolError(
                "resume_session_not_found",
                "Retained session log was not found.",
                request_id=request.id,
            )
        try:
            checkpoint_lineage = None
            if shutil.which("git") is None and not checkpoint_storage_exists(session.root):
                # A fresh machine can restore a conversation without Git. Existing checkpoint
                # state must still take the normal adoption path and fail closed if unavailable.
                session.surface.emit_warning(
                    "Conversation history can be restored, but file checkpoints need Git. "
                    "Install Git to use checkpoint recovery."
                )
            else:
                checkpoint_lineage = self._change_ledger_for_session(session).adopt_session(
                    session.session_id,
                    normalized_target,
                )
        except ChangeLedgerError as exc:
            raise ProtocolError(
                "checkpoint_recovery_failed", str(exc), request_id=request.id
            ) from exc
        messages = _load_chat_resume_messages(target_path)
        total_history_messages = len(messages)
        full_history = _optional_bool(params, "full_history", default=False, request_id=request.id)
        replay_messages = (
            messages[-max_messages:]
            if not full_history and total_history_messages > max_messages
            else messages
        )
        replay_messages = [_redact_resume_message(message) for message in replay_messages]
        target_messages = getattr(session.agent_session, "messages", None)
        if not isinstance(target_messages, list):
            raise ProtocolError(
                "resume_not_supported",
                "Live session does not expose mutable message history for resume replay.",
                request_id=request.id,
            )
        before_count = len(target_messages)
        bounded = total_history_messages > len(replay_messages)
        resume_context_message = (
            None if bounded else _build_chat_resume_context_message(target_path)
        )
        context_message = resume_context_message
        context_inserted = False
        if context_message:
            context_inserted = _insert_chat_resume_context_message(
                session.agent_session,
                str(redact_secrets(context_message)),
            )
        target_messages.extend(replay_messages)
        after_count = len(target_messages)
        store = getattr(session.agent_session, "store", None)
        if (
            store is not None
            and callable(getattr(store, "append", None))
            and _optional_bool(params, "persist", default=True, request_id=request.id)
        ):
            startup = getattr(session.agent_session, "startup_messages", [])
            retained_messages = [message for message in target_messages if message not in startup]
            store.append(
                "conversation_summary_updated", {"active_conversation_messages": retained_messages}
            )
        if _optional_bool(params, "emit_history", default=False, request_id=request.id):
            for message in replay_messages:
                if _is_chat_resume_context_message(message):
                    continue
                if message.get("role") in {"user", "assistant"} and isinstance(
                    message.get("content"), str
                ):
                    session.surface.emit_message_end(message["content"], role=message["role"])
        if normalized_target != session.session_id and not session.sensitive_media:
            # Like the terminal's /resume, the resumed session's persona (mode, write
            # scope, model role) comes back with its history. A session that continues
            # its own log restored it at session.create. Never under a running task.
            with self._state_lock:
                _reconcile_session_job_state(session)
                persona_busy = _job_is_active(session.active_job)
            chat_loop = _ensure_chat_loop_facade()
            if not persona_busy and chat_loop._load_resumed_persona(
                session.agent_session, target_path
            ):
                chat_loop._reapply_resumed_persona(
                    session=session.agent_session, console=None, log_path=target_path
                )
                session.mode = str(
                    getattr(session.agent_session, "mode", session.mode) or session.mode
                )
                session.persona_source = "resume"
                session.surface.emit_status_update(
                    mode=session.mode,
                    model=str(
                        getattr(getattr(session.agent_session, "cfg", None), "model", "") or ""
                    ),
                )
        # Verification provenance follows the adopted conversation lineage.
        from ..cli_impl.commands.chat_resume_helpers import _restore_chat_resume_evidence

        _restore_chat_resume_evidence(
            session.agent_session, path=target_path, source_session_id=normalized_target
        )
        try:
            queue_recovery = self._prompt_queue.rebind_recoverable(
                source_session_id=normalized_target,
                target_session_id=session.session_id,
            )
        except PromptQueueError as exc:
            raise ProtocolError(
                "prompt_queue_recovery_failed", str(exc), request_id=request.id
            ) from exc
        return {
            "session_id": session.session_id,
            "resumed_session_id": normalized_target,
            "resumed": True,
            "message": "Retained session context was replayed into the live IDE session from the bounded session log.",
            "history_count": len(replay_messages),
            "history_count_total": total_history_messages,
            "bounded": bounded,
            "max_messages": max_messages,
            "source": "retained_session_log_replay",
            "model_context_replay_supported": True,
            "resume_context_loaded": context_inserted,
            "resume_context_skipped_reason": "bounded_replay" if bounded else None,
            "messages_before": before_count,
            "messages_after": after_count,
            "queued_prompts_rebound": queue_recovery["pending"],
            "expired_prompts_recovered": queue_recovery["recovered"],
            "active_prompts_observed": queue_recovery["active"],
            "checkpoint_lineage_adopted": checkpoint_lineage is not None,
            "checkpoint_lineage_session_id": checkpoint_lineage,
        }

    def _session_images_list(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        with self._state_lock:
            return _session_images_payload(session)

    def _session_images_add(self, request: ProtocolRequest) -> dict[str, Any]:
        params = request.params
        session = self._require_session(
            _required_str(params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        if session.sensitive_media:
            raise ProtocolError(
                "sensitive_media_restricted",
                "Sensitive host-context sessions accept an optional bound image only on chat.send.",
                request_id=request.id,
            )
        image_paths = _image_paths_param(params, workspace_root=session.root, request_id=request.id)
        if not image_paths:
            raise ProtocolError(
                "missing_field",
                "session.images.add requires images or image_paths.",
                request_id=request.id,
            )
        replace = _optional_bool(params, "replace", default=False, request_id=request.id)
        with self._state_lock:
            before = len(session.pending_images)
            if replace:
                session.pending_images = []
            session.pending_images = _dedupe_paths([*session.pending_images, *image_paths])
            payload = _session_images_payload(session)
            payload.update(
                {
                    "added_count": len(session.pending_images) - (0 if replace else before),
                    "replaced": replace,
                }
            )
            return payload

    def _session_images_clear(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        with self._state_lock:
            count_before = len(session.pending_images)
            session.pending_images.clear()
            return {
                "session_id": session.session_id,
                "cleared": True,
                "count_before": count_before,
                "count_after": 0,
                "images": [],
            }

    def _session_set_mode(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        mode = _mode_param(request.params, request_id=request.id)
        # The stdio protocol has no next-message staging handshake. Keep its
        # contract explicit: an idle session may switch immediately; an active
        # turn is rejected instead of rebuilding the tools underneath it.
        with self._state_lock:
            _reconcile_session_job_state(session)
            if _job_is_active(session.active_job):
                raise ProtocolError(
                    "session_busy",
                    "Cancel or wait for the active turn before changing Permissions.",
                    request_id=request.id,
                )
            _apply_agent_session_mode(session.agent_session, mode)
            session.mode = str(getattr(session.agent_session, "mode", mode) or mode)
        session.surface.emit_status_update(
            mode=session.mode,
            model=str(getattr(getattr(session.agent_session, "cfg", None), "model", "") or ""),
        )
        return _session_status_payload(session)

    def _session_set_model(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        if session.sensitive_media:
            raise ProtocolError(
                "computer_use_destination_mismatch",
                "A sensitive host-context session has an immutable model route.",
                request_id=request.id,
            )
        cfg = getattr(session.agent_session, "cfg", None)
        if cfg is None:
            raise ProtocolError(
                "config_error", "Session config is unavailable.", request_id=request.id
            )
        model = _required_str(request.params, "model", request_id=request.id)
        cfg.model = model
        base_url = _optional_str(request.params, "base_url", request_id=request.id)
        if base_url:
            _apply_config_overrides(cfg, {"base_url": base_url}, request_id=request.id)
        _refresh_agent_session_config(session.agent_session, cfg)
        session.surface.emit_status_update(mode=session.mode, model=model)
        return _session_status_payload(session)

    def _session_set_profile(self, request: ProtocolRequest) -> dict[str, Any]:
        from ..profiles import get_profile
        from .provider_switch import switch_session_provider

        self._require_workspace_trusted(request)
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        name = _required_str(request.params, "name", request_id=request.id)
        model = _optional_str(request.params, "model", request_id=request.id)
        with self._state_lock:
            _reconcile_session_job_state(session)
            if _job_is_active(session.active_job):
                raise ProtocolError(
                    "session_busy",
                    "Finish or stop the current task before switching providers.",
                    request_id=request.id,
                )
            saved_cfg = clone_cfg(load_config())
            profile = get_profile(saved_cfg, name)
            if profile is None:
                raise ProtocolError(
                    "config_error", f"Unknown provider profile: {name}", request_id=request.id
                )
            if model:
                profile = replace(profile, default_model=model)
                add_profile(saved_cfg, profile, allow_auth_profile_update=True)
            set_active_profile(saved_cfg, name)
            # Keep this conversation's permissions, budgets, and workspace settings.
            cfg = clone_cfg(session.agent_session.cfg)
            add_profile(cfg, profile, allow_auth_profile_update=True)
            set_active_profile(cfg, name)
            switch_session_provider(
                session.agent_session,
                cfg,
                refresh=_refresh_agent_session_config,
                persist=lambda: save_config(saved_cfg),
            )
        session.surface.emit_status_update(mode=session.mode, model=cfg.model)
        return _session_status_payload(session)

    def _session_set_stream(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        cfg = getattr(session.agent_session, "cfg", None)
        if cfg is None:
            raise ProtocolError(
                "config_error", "Session config is unavailable.", request_id=request.id
            )
        cfg.stream = _required_bool(request.params, "stream", request_id=request.id)
        _refresh_agent_session_config(session.agent_session, cfg)
        session.surface.emit_status_update(
            mode=session.mode, model=str(getattr(cfg, "model", "") or "")
        )
        return _session_status_payload(session)

    def _session_set_active_workdir(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        path = _required_str(request.params, "path", request_id=request.id)
        payload = set_session_active_workdir(
            session.agent_session,
            path,
            source="ide_protocol",
        )
        return {"session_id": session.session_id, **payload}

    def _session_model_info(self, request: ProtocolRequest) -> dict[str, Any]:
        params = request.params
        session = self._require_session(
            _required_str(params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        cfg = getattr(session.agent_session, "cfg", None)
        if cfg is None:
            cfg = clone_cfg(load_config())
        explicit_model = _optional_str(params, "model", request_id=request.id)
        if session.sensitive_media and explicit_model is not None:
            raise ProtocolError(
                "computer_use_destination_mismatch",
                "A sensitive host-context session has an immutable model route.",
                request_id=request.id,
            )
        live_destination: ComputerUseDestinationBinding | None = None
        if session.sensitive_media:
            _assert_live_computer_use_destination(session, request_id=request.id)
            live_destination = _actual_computer_use_destination(
                session.agent_session,
                request_id=request.id,
            )
        model = str(explicit_model or getattr(cfg, "model", "") or "").strip()
        if live_destination is not None:
            model = live_destination.model
        if not model:
            raise ProtocolError("missing_field", "model is unavailable.", request_id=request.id)
        profile = str(getattr(cfg, "profile", "") or "")
        active_profile = ""
        extra = getattr(cfg, "extra_fields", None)
        if isinstance(extra, dict):
            active_profile = str(extra.get("active_profile") or "")
        base_url = str(getattr(cfg, "base_url", "") or "")
        registry = getattr(session.agent_session, "model_registry", None)
        registry_unknown = False
        if registry is None:
            try:
                from ..model_registry import ModelRegistry

                registry = ModelRegistry(cfg=cfg, api_key=None)
            except Exception:
                registry_unknown = True
        metadata = None
        if registry is not None:
            try:
                metadata = registry.get(model)
            except Exception:
                registry_unknown = True
        return {
            "session_id": session.session_id,
            "model": str(redact_secrets(model)),
            "provider": (
                live_destination.profile
                if live_destination is not None
                else active_profile or profile or str(getattr(cfg, "provider", "") or "")
            ),
            "profile": live_destination.profile
            if live_destination is not None
            else active_profile or profile or None,
            # The generic IDE method historically returned a redacted endpoint. A
            # sensitive host-context route returns only its digest commitment, even
            # to the main process, so it can never become renderer-visible by error.
            "base_url": None
            if live_destination is not None
            else str(redact_secrets(base_url))
            if base_url
            else None,
            "base_url_redacted": bool(base_url),
            "context_window": (
                int(metadata.context_window_tokens)
                if metadata is not None
                else _optional_int_attr(cfg, "context_window")
            ),
            # Host image transport is fail-closed: an unknown model must not be
            # treated as image-capable merely because the IDE method exists.
            "vision_support": bool(metadata.supports_vision) if metadata is not None else False,
            "tool_support": bool(
                getattr(
                    getattr(session.agent_session, "client", None), "supports_tool_calling", False
                )
            ),
            "streaming_support": bool(getattr(cfg, "stream", False)),
            "source": "config" if explicit_model else "active_session",
            "source_metadata": {
                "provider_reported": False,
                "config": True,
                "registry": metadata is not None,
                "unknown": registry_unknown or metadata is None,
            },
            "secret_values_included": False,
            **(
                {
                    "computer_use_destination": live_destination.to_protocol(),
                    "route_fingerprint": live_destination.route_fingerprint,
                }
                if live_destination is not None
                else {}
            ),
        }

    def _session_personas_list(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        cfg = getattr(session.agent_session, "cfg", None)
        if not persona_modes_enabled(cfg):
            # An honest disabled snapshot, not an error: the IDE renders the
            # kill-switch state instead of a failure.
            return {
                "session_id": session.session_id,
                "enabled": False,
                "active": DEFAULT_PERSONA,
                "active_source": session.persona_source,
                "personas": [],
            }
        registry = getattr(session.agent_session, "persona_registry", None)
        return {
            "session_id": session.session_id,
            "enabled": True,
            "active": normalize_persona(
                getattr(session.agent_session, "persona", DEFAULT_PERSONA), registry
            ),
            "active_source": session.persona_source,
            "personas": [
                _persona_definition_payload(definition)
                for definition in _ordered_persona_definitions(registry)
            ],
        }

    def _session_persona_set(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        cfg = getattr(session.agent_session, "cfg", None)
        # Guard order mirrors the /persona chat command exactly: enabled
        # check, name validation, mid-job block, no-op short circuit, then the
        # chat loop's shared application primitive.
        if not persona_modes_enabled(cfg):
            raise ProtocolError(
                "persona_modes_disabled",
                "Persona modes are disabled.",
                request_id=request.id,
            )
        requested = _required_str(request.params, "persona", request_id=request.id).lower()
        registry = getattr(session.agent_session, "persona_registry", None)
        if not is_persona_name(requested, registry):
            valid = ", ".join(
                definition.name for definition in _ordered_persona_definitions(registry)
            )
            raise ProtocolError(
                "invalid_persona",
                f"Unknown persona. Valid personas: {valid}.",
                request_id=request.id,
            )
        # The chat loop never swaps the tool surface mid-turn; the bridge must
        # not rebuild it under a running job either.
        with self._state_lock:
            _reconcile_session_job_state(session)
            if _job_is_active(session.active_job):
                raise ProtocolError(
                    "persona_change_busy",
                    "Cannot change persona while a task is running.",
                    request_id=request.id,
                )
        target = normalize_persona(requested, registry)
        definition = get_persona(target, registry)
        current = normalize_persona(
            getattr(session.agent_session, "persona", DEFAULT_PERSONA), registry
        )
        if target == current:
            # Same persona: succeed without reapplying and without emitting
            # a persona_changed event.
            return {
                "session_id": session.session_id,
                "persona": current,
                "effective_mode": str(
                    getattr(session.agent_session, "mode", session.mode) or session.mode
                ),
                "model_role": definition.model_role,
                "changed": False,
            }
        try:
            # The shared chat-loop primitive owns clamp, write-scope
            # narrowing/restore, the sticky persona model swap, the
            # environment-context refresh, and the persona_changed emission
            # through this session's protocol surface (exactly once).
            effective_mode = _apply_agent_session_persona(
                session.agent_session, persona=target, source="user"
            )
        except Exception as exc:  # noqa: BLE001 - swap/rebuild errors may embed provider details
            raise ProtocolError(
                "persona_change_failed",
                "Failed to change persona.",
                request_id=request.id,
            ) from exc
        session.mode = str(getattr(session.agent_session, "mode", effective_mode) or effective_mode)
        session.persona_source = "user"
        session.surface.emit_status_update(
            mode=session.mode,
            model=str(getattr(getattr(session.agent_session, "cfg", None), "model", "") or ""),
        )
        return {
            "session_id": session.session_id,
            "persona": definition.name,
            "effective_mode": effective_mode,
            "model_role": definition.model_role,
            "changed": True,
        }

    def _session_subagents_status(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        return _session_subagents_payload(session)

    def _session_subagents_set_enabled(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        enabled = _required_bool(request.params, "enabled", request_id=request.id)
        cfg = getattr(session.agent_session, "cfg", None)
        previous = bool(getattr(session.agent_session, "subagents_enabled", False))
        session.agent_session.subagents_enabled = enabled
        if cfg is not None:
            cfg.subagents_enabled = enabled
            session.agent_session.cfg = cfg
        payload = _session_subagents_payload(session)
        payload.update(
            {
                "changed": previous != enabled,
                "previous_enabled": previous,
                "audit": {
                    "changed_fields": ["subagents_enabled"] if previous != enabled else [],
                    "source": "ide_protocol",
                    "secret_values_included": False,
                },
            }
        )
        return payload

    def _session_trace_status(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        with self._state_lock:
            retained = len(self._trace_visible_events_locked(session.session_id))
        return _session_trace_status_payload(session, retained)

    def _session_trace_set_level(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        level = _trace_level_param(request.params, request_id=request.id)
        if level == "full":
            self._require_confirmation(request)
        previous = _session_trace_level(session)
        applied = _set_session_trace_level(session, level)
        with self._state_lock:
            retained = len(self._trace_visible_events_locked(session.session_id))
        payload = _session_trace_status_payload(session, retained)
        payload.update(
            {
                "changed": previous != applied,
                "previous_level": previous,
                "full_trace_confirmed": level == "full",
                "audit": {
                    "changed_fields": ["trace_level"] if previous != applied else [],
                    "secret_values_included": False,
                    "source": "ide_protocol",
                },
            }
        )
        return payload

    def _session_trace_list_events(self, request: ProtocolRequest) -> dict[str, Any]:
        session_id = _required_str(request.params, "session_id", request_id=request.id)
        session = self._require_session(session_id, request_id=request.id)
        max_events = _bounded_int(
            request.params.get("max_events"),
            default=EVENT_REPLAY_RESPONSE_MAX,
            upper=TRACE_EVENT_RESPONSE_MAX,
            request_id=request.id,
        )
        max_bytes = _bounded_int(
            request.params.get("max_bytes"),
            default=TRACE_ARTIFACT_DEFAULT_MAX_BYTES,
            upper=TRACE_ARTIFACT_MAX_BYTES,
            request_id=request.id,
        )
        after_sequence = _optional_int(
            request.params.get("after_sequence"),
            request_id=request.id,
        )
        with self._state_lock:
            retained = self._trace_visible_events_locked(session.session_id)
        result = _bounded_trace_events(
            retained,
            max_events=max_events,
            max_bytes=max_bytes,
            after_sequence=after_sequence,
        )
        result.update(
            {
                "session_id": session.session_id,
                "level": _session_trace_level(session),
                "redacted": True,
                "secret_values_included": False,
                "max_events": max_events,
                "max_bytes": max_bytes,
            }
        )
        return result

    def _session_trace_read_artifact(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        artifact_id = _required_str(request.params, "artifact_id", request_id=request.id)
        max_bytes = _bounded_int(
            request.params.get("max_bytes"),
            default=TRACE_ARTIFACT_DEFAULT_MAX_BYTES,
            upper=TRACE_ARTIFACT_MAX_BYTES,
            request_id=request.id,
        )
        try:
            payload = session.artifact_store.read(artifact_id, max_bytes=max_bytes)
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        content = str(payload.get("content") or "")
        redacted = str(redact_secrets(content))
        payload.update(
            {
                "session_id": session.session_id,
                "content": redacted,
                "redacted": True,
                "secret_values_included": False,
                "max_bytes": max_bytes,
            }
        )
        return payload

    def _session_trace_clear(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        # Clearing the trace only advances the per-session visibility floor.
        # The underlying event buffer is the session.getEvents reconnect
        # replay ring and must survive a trace clear.
        with self._state_lock:
            visible_before = len(self._trace_visible_events_locked(session.session_id))
            buffer = self._event_buffers.get(session.session_id)
            highest = int(buffer[-1]["sequence"]) if buffer else 0
            previous_floor = self._trace_view_floors.get(session.session_id, 0)
            self._trace_view_floors[session.session_id] = max(previous_floor, highest)
        return {
            "session_id": session.session_id,
            "cleared": True,
            "events_before": visible_before,
            "events_after": 0,
            "replay_preserved": True,
            "redacted": True,
            "secret_values_included": False,
        }

    def _session_terminals_list(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        manager = _terminal_manager(session)
        if manager is None:
            return _terminals_unavailable_payload(session)
        try:
            summaries = manager.list()
        except Exception as exc:  # noqa: BLE001
            raise ProtocolError(
                "terminals_unavailable",
                str(redact_secrets(f"Terminal manager failed: {exc}")),
                request_id=request.id,
            ) from exc
        return {
            "session_id": session.session_id,
            "supported": True,
            "available": True,
            "terminals": [
                _terminal_summary_payload(summary, session.root) for summary in summaries
            ],
            "count": len(summaries),
            "redacted": True,
            "secret_values_included": False,
            "arbitrary_shell_execution": False,
            "interactive_pty_streaming": False,
        }

    def _session_terminals_show(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        process_id = _required_str(request.params, "process_id", request_id=request.id)
        manager = _terminal_manager(session)
        if manager is None:
            payload = _terminals_unavailable_payload(session)
            payload.update({"process_id": process_id, "lines": []})
            return payload
        since = _optional_int(request.params.get("since"), request_id=request.id) or 0
        max_lines = _bounded_int(
            request.params.get("max_lines"),
            default=TERMINAL_OUTPUT_DEFAULT_MAX_LINES,
            upper=TERMINAL_OUTPUT_RESPONSE_MAX_LINES,
            request_id=request.id,
        )
        max_bytes = _bounded_int(
            request.params.get("max_bytes"),
            default=TERMINAL_OUTPUT_DEFAULT_MAX_BYTES,
            upper=TERMINAL_OUTPUT_MAX_BYTES,
            request_id=request.id,
        )
        try:
            snapshot = manager.read(process_id, since=since)
        except KeyError as exc:
            raise ProtocolError(
                "terminal_not_found", "Background terminal was not found.", request_id=request.id
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ProtocolError(
                "terminals_unavailable",
                str(redact_secrets(f"Terminal read failed: {exc}")),
                request_id=request.id,
            ) from exc
        return _terminal_snapshot_payload(
            session,
            snapshot,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    def _session_terminals_kill(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        self._require_confirmation(request)
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        process_id = _required_str(request.params, "process_id", request_id=request.id)
        manager = _terminal_manager(session)
        if manager is None:
            payload = _terminals_unavailable_payload(session)
            payload.update({"process_id": process_id, "killed": False})
            return payload
        try:
            snapshot = manager.kill(process_id)
        except KeyError as exc:
            raise ProtocolError(
                "terminal_not_found", "Background terminal was not found.", request_id=request.id
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ProtocolError(
                "terminal_kill_failed",
                str(redact_secrets(f"Terminal kill failed: {exc}")),
                request_id=request.id,
            ) from exc
        payload = _terminal_snapshot_payload(
            session,
            snapshot,
            max_lines=TERMINAL_OUTPUT_DEFAULT_MAX_LINES,
            max_bytes=TERMINAL_OUTPUT_DEFAULT_MAX_BYTES,
        )
        payload.update({"killed": True})
        return payload

    def _session_terminals_clear(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        self._require_confirmation(request)
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        process_id = _required_str(request.params, "process_id", request_id=request.id)
        manager = _terminal_manager(session)
        if manager is None:
            payload = _terminals_unavailable_payload(session)
            payload.update({"process_id": process_id, "cleared": False})
            return payload
        clear_fn = getattr(manager, "clear", None)
        if not callable(clear_fn):
            return {
                "session_id": session.session_id,
                "process_id": process_id,
                "supported": False,
                "available": True,
                "cleared": False,
                "reason": "The active terminal manager does not support clearing retained output.",
                "redacted": True,
                "secret_values_included": False,
                "arbitrary_shell_execution": False,
                "interactive_pty_streaming": False,
            }
        try:
            result = clear_fn(process_id)
        except KeyError as exc:
            raise ProtocolError(
                "terminal_not_found", "Background terminal was not found.", request_id=request.id
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ProtocolError(
                "terminal_clear_failed",
                str(redact_secrets(f"Terminal clear failed: {exc}")),
                request_id=request.id,
            ) from exc
        return {
            "session_id": session.session_id,
            "process_id": process_id,
            "supported": True,
            "available": True,
            "cleared": True,
            "result": redact_secrets(result),
            "redacted": True,
            "secret_values_included": False,
            "arbitrary_shell_execution": False,
            "interactive_pty_streaming": False,
        }

    def _session_clear(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        with self._state_lock:
            _reconcile_session_job_state(session)
            if _job_is_active(session.active_job):
                raise ProtocolError(
                    "session_busy",
                    "Cancel or wait for the active turn before clearing this session.",
                    request_id=request.id,
                )
        try:
            cancelled_prompts = self._prompt_queue.cancel_pending_for_session(
                session_id=session.session_id
            )
        except PromptQueueError as exc:
            raise ProtocolError("prompt_queue_error", str(exc), request_id=request.id) from exc
        messages = getattr(session.agent_session, "messages", None)
        before = len(messages) if isinstance(messages, list) else 0
        if isinstance(messages, list):
            messages.clear()
        with self._state_lock:
            session.pending_images.clear()
            _hold_unread_steers(session)
            session.unread_steers.clear()
        self._clear_session_approvals(session)
        self._clear_session_host_actions(session)
        after = len(messages) if isinstance(messages, list) else 0
        return {
            "session_id": session.session_id,
            "cleared": isinstance(messages, list),
            "messages_before": before,
            "messages_after": after,
            "queued_prompts_cancelled": cancelled_prompts,
        }

    def _session_cancel(self, request: ProtocolRequest) -> dict[str, Any]:
        session_id = _required_str(request.params, "session_id", request_id=request.id)
        session = self._require_session(session_id, request_id=request.id, allow_closing=True)
        reason = (
            _optional_str(request.params, "reason", request_id=request.id) or "cancelled_by_user"
        )
        close_when_idle = _optional_bool(
            request.params,
            "close_when_idle",
            default=False,
            request_id=request.id,
        )
        cancelled_prompts = 0
        if close_when_idle:
            try:
                cancelled_prompts = self._prompt_queue.cancel_pending_for_session(
                    session_id=session.session_id
                )
            except PromptQueueError as exc:
                raise ProtocolError("prompt_queue_error", str(exc), request_id=request.id) from exc
        if session.sensitive_media:
            with self._state_lock:
                active_binding = (
                    session.active_job.host_image_binding
                    if session.active_job is not None
                    else None
                )
            reason = _redact_bridge_error(
                session,
                reason,
                host_image_binding=active_binding,
            )
        cancellation_status: str | None = None
        cancellation_job: BridgeJob | None = None
        with self._state_lock:
            _reconcile_session_job_state(session)
            job = session.active_job
            if _job_is_active(job):
                assert job is not None
                if close_when_idle:
                    session.close_when_idle = True
                result = _request_job_cancellation(job, reason)
                cancellation_status = str(result["status"])
                cancellation_job = job
                response = {
                    "session_id": session.session_id,
                    "status": result["status"],
                    "state": result["state"],
                    "job_id": job.job_id,
                    "job": _job_summary(job),
                    "close_when_idle": session.close_when_idle,
                    "queued_prompts_cancelled": cancelled_prompts,
                }
            else:
                response = None
        if response is not None and cancellation_job is not None:
            if cancellation_status == "cancellation_requested":
                self._cancel_pending_approvals(session, reason)
                self._cancel_pending_host_actions(session, reason)
                self._cancel_pending_computer_use(session, reason)
                session.surface.emit_warning(
                    f"cancellation_requested {cancellation_job.job_id} "
                    f"reason={cancellation_job.cancellation_reason}"
                )
            elif cancellation_status == "non_cancellable":
                session.surface.emit_warning(
                    f"cancel_rejected {cancellation_job.job_id} non_cancellable"
                )
            if close_when_idle:
                self._schedule_session_close_after_job(session, cancellation_job)
            return response
        try:
            self._close_session(session)
        except BridgeSessionCleanupError as exc:
            raise ProtocolError(
                "browser_cleanup_incomplete",
                str(exc),
                request_id=request.id,
            ) from exc
        return {
            "session_id": session.session_id,
            "status": "closed",
            "state": "closed",
            "job": None,
            "close_when_idle": False,
            "queued_prompts_cancelled": cancelled_prompts,
        }

    def _schedule_session_close_after_job(self, session: BridgeSession, job: BridgeJob) -> None:
        """Close an explicitly abandoned session after its exact active worker settles."""

        with self._state_lock:
            existing = session.close_worker
            if existing is not None and existing.is_alive():
                return
            worker = threading.Thread(
                target=self._close_session_after_job,
                args=(session, job),
                name=f"alysis-ide-close-{session.session_id[:12]}",
                daemon=True,
            )
            session.close_worker = worker
        try:
            worker.start()
        except Exception as exc:  # noqa: BLE001 - OS resource pressure
            with self._state_lock:
                if session.close_worker is worker:
                    session.close_worker = None
            session.surface.emit_error(
                "session_close_schedule_failed",
                str(redact_secrets(f"Could not schedule abandoned session cleanup: {exc}")),
                False,
            )

    def _close_session_after_job(self, session: BridgeSession, job: BridgeJob) -> None:
        thread = job.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

        cleanup_error: BridgeSessionCleanupError | None = None
        try:
            with self._state_lock:
                _reconcile_session_job_state(session)
                active = session.active_job
            if _job_is_active(active):
                # Defensive fail-closed guard: never tear resources out from
                # under a replacement worker if an unexpected producer raced
                # the close request.
                return
            for attempt in range(2):
                try:
                    self._close_session(session)
                    cleanup_error = None
                    break
                except BridgeSessionCleanupError as exc:
                    cleanup_error = exc
                    if attempt == 0:
                        continue
            if cleanup_error is not None:
                session.surface.emit_error("session_cleanup_incomplete", str(cleanup_error), False)
        finally:
            with self._state_lock:
                if session.close_worker is threading.current_thread():
                    session.close_worker = None

    def _host_action_dispatched(self, request: ProtocolRequest) -> dict[str, Any]:
        if set(request.params) != {
            "session_id",
            "host_action_id",
            "workspace_fence",
            "capability_fingerprint",
        }:
            raise ProtocolError(
                "invalid_host_dispatch",
                "Unexpected dispatch receipt fields.",
                request_id=request.id,
            )
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        key = _required_str(request.params, "host_action_id", request_id=request.id)
        with session.host_action_lock:
            pending = session.pending_host_actions.get(key)
            if (
                session.host_approval_protocol != 1
                or pending is None
                or pending.resolved
                or pending.phase != "execute"
                or time.monotonic() >= pending.deadline
                or request.params.get("workspace_fence") != pending.workspace_fence
                or request.params.get("capability_fingerprint") != pending.capability_fingerprint
                or session.active_job is None
                or session.active_job.job_id != pending.job_id
                or session.active_job.cancellation_event.is_set()
            ):
                raise ProtocolError(
                    "stale_host_action",
                    "Dispatch authority is no longer current.",
                    request_id=request.id,
                )
            if pending.dispatched:
                raise ProtocolError(
                    "duplicate_host_dispatch",
                    "This action already crossed its dispatch boundary.",
                    request_id=request.id,
                )
            pending.dispatched = True
        return {"status": "dispatched", "host_action_id": key}

    def _host_action_respond(self, request: ProtocolRequest) -> dict[str, Any]:
        unsupported_fields = sorted(
            set(request.params)
            - {
                "session_id",
                "host_action_id",
                "workspace_fence",
                "capability_fingerprint",
                "ok",
                "result",
                "error",
            }
        )
        if unsupported_fields:
            raise ProtocolError(
                "invalid_host_action_response",
                "Host action response contains unsupported fields.",
                request_id=request.id,
            )
        session_id = _required_str(request.params, "session_id", request_id=request.id)
        host_action_id = _required_str(request.params, "host_action_id", request_id=request.id)
        _validate_host_action_id(host_action_id, request_id=request.id)
        workspace_fence = _required_str(request.params, "workspace_fence", request_id=request.id)
        _validate_workspace_fence(workspace_fence, request_id=request.id)
        capability_fingerprint = _required_str(
            request.params, "capability_fingerprint", request_id=request.id
        )
        _validate_host_capability_fingerprint(capability_fingerprint, request_id=request.id)
        ok = _required_bool(request.params, "ok", request_id=request.id)
        session = self._require_session(session_id, request_id=request.id, allow_closing=True)
        if not secrets.compare_digest(workspace_fence, session.workspace_fence):
            raise ProtocolError(
                "host_action_workspace_fence_mismatch",
                "Host action response does not match the owning workspace fence.",
                request_id=request.id,
            )
        if not secrets.compare_digest(capability_fingerprint, session.host_capability_fingerprint):
            raise ProtocolError(
                "host_action_capability_fence_mismatch",
                "Host action response does not match the negotiated capability set.",
                request_id=request.id,
            )

        raw_result = request.params.get("result")
        raw_error = request.params.get("error")
        if ok:
            if not isinstance(raw_result, dict) or raw_error is not None:
                raise ProtocolError(
                    "invalid_host_action_response",
                    "Successful host action responses require result and must omit error.",
                    request_id=request.id,
                )
        elif not isinstance(raw_error, dict) or raw_result is not None:
            raise ProtocolError(
                "invalid_host_action_response",
                "Failed host action responses require error and must omit result.",
                request_id=request.id,
            )

        with session.host_action_lock:
            self._prune_resolved_host_actions_locked(session, time.monotonic())
            pending = session.pending_host_actions.get(host_action_id)
            if pending is None:
                resolved = session.resolved_host_actions.get(host_action_id)
                if resolved is None:
                    raise ProtocolError(
                        "unknown_host_action",
                        "No pending host action exists with that id.",
                        request_id=request.id,
                    )
                code = (
                    "duplicate_host_action_response"
                    if resolved.status in {"completed", "failed"}
                    else "stale_host_action_response"
                )
                raise ProtocolError(
                    code,
                    "Host action was already resolved and this response was rejected.",
                    request_id=request.id,
                )
            if pending.resolved:
                code = (
                    "duplicate_host_action_response"
                    if pending.status in {"completed", "failed"}
                    else "stale_host_action_response"
                )
                raise ProtocolError(
                    code,
                    "Host action was already resolved and this response was rejected.",
                    request_id=request.id,
                )
            if time.monotonic() > pending.deadline:
                self._resolve_host_action_error_locked(
                    session,
                    pending,
                    HostActionError(
                        "host_action_timeout",
                        "The dispatched host write may have completed; inspect its outcome before retrying."
                        if pending.write_effect
                        and (pending.dispatched or session.host_approval_protocol != 1)
                        else "The IDE host action response arrived after its deadline.",
                        retryable=not pending.write_effect
                        or (session.host_approval_protocol == 1 and not pending.dispatched),
                        dispatch_state="dispatched"
                        if pending.dispatched
                        else "not_dispatched"
                        if session.host_approval_protocol == 1
                        else "unknown",
                    ),
                    status="expired",
                )
                self._emit_host_action_cancelled(session, pending, "deadline_exceeded")
                raise ProtocolError(
                    "stale_host_action_response",
                    "Host action response arrived after its deadline and was rejected.",
                    request_id=request.id,
                )

            try:
                if ok:
                    assert isinstance(raw_result, dict)
                    if pending.phase == "discard" and session.host_approval_protocol == 1:
                        if raw_result != {"discarded": True}:
                            raise HostActionError(
                                "invalid_host_preparation", "Invalid discard receipt."
                            )
                        normalized_result = {"discarded": True}
                    elif pending.phase == "prepare" and session.host_approval_protocol == 1:
                        if set(raw_result) != {
                            "preparation_id",
                            "required",
                            "write_effect",
                            "kind",
                            "reason",
                            "preview",
                            "target",
                        } and set(raw_result) != {
                            "preparation_id",
                            "required",
                            "write_effect",
                            "kind",
                            "reason",
                            "preview",
                            "target",
                            "context",
                        }:
                            raise HostActionError(
                                "invalid_host_preparation", "Unexpected host preparation fields."
                            )
                        normalized_result = _json_clone(raw_result)
                        if "context" in normalized_result:
                            try:
                                normalized_result["context"] = normalize_connector_approval_context(
                                    normalized_result["context"]
                                )
                            except ValueError as exc:
                                raise HostActionError(
                                    "invalid_host_preparation",
                                    str(exc),
                                    dispatch_state="not_dispatched",
                                ) from exc
                        if json_size_bytes(normalized_result) > HOST_ACTION_MAX_RESULT_BYTES:
                            raise HostActionError(
                                "invalid_host_preparation", "Host preparation is too large."
                            )
                    else:
                        normalized_result = normalize_host_action_result(pending.action, raw_result)
                    pending.result = normalized_result
                    pending.status = "completed"
                else:
                    assert isinstance(raw_error, dict)
                    host_code, host_message, retryable = normalize_host_error(raw_error)
                    pending.error = HostActionError(
                        "host_action_failed",
                        str(redact_secrets(host_message)),
                        retryable=retryable and not pending.write_effect,
                        host_error_code=host_code,
                        dispatch_state="dispatched"
                        if pending.dispatched
                        else "not_dispatched"
                        if session.host_approval_protocol == 1
                        else "unknown",
                    )
                    pending.status = "failed"
            except HostActionError as exc:
                raise ProtocolError(exc.code, exc.message, request_id=request.id) from exc

            pending.resolved = True
            session.resolved_host_actions[host_action_id] = ResolvedHostActionRecord(
                status=pending.status,
                resolved_at=time.monotonic(),
            )
            pending.done.set()
            return {
                "status": "applied",
                "session_id": session.session_id,
                "host_action_id": pending.host_action_id,
                "action": pending.action,
                "outcome": "result" if ok else "error",
            }

    def _permission_rules_list(self, request: ProtocolRequest) -> dict[str, Any]:
        _ = request
        try:
            rules = self._permission_policy_store.list_rules()
        except PermissionPolicyError as exc:
            raise ProtocolError("permission_policy_error", str(exc), request_id=request.id) from exc
        return {
            "rules": [rule.to_public_dict() for rule in rules],
            "command_patterns_redacted": True,
        }

    def _permission_rules_grant(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_confirmation(request)
        effect = _required_str(request.params, "effect", request_id=request.id)
        try:
            rule = self._permission_policy_store.grant(
                effect,
                tool_pattern=_optional_str(request.params, "tool_pattern", request_id=request.id),
                path_pattern=_optional_str(request.params, "path_pattern", request_id=request.id),
                command_pattern=_optional_str(
                    request.params, "command_pattern", request_id=request.id
                ),
                source="ide_user",
            )
        except PermissionPolicyError as exc:
            raise ProtocolError("invalid_permission_rule", str(exc), request_id=request.id) from exc
        return {"status": "granted", "rule": rule.to_public_dict()}

    def _permission_rules_revoke(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_confirmation(request)
        rule_id = _required_str(request.params, "rule_id", request_id=request.id)
        try:
            removed = self._permission_policy_store.revoke(rule_id)
        except PermissionPolicyError as exc:
            raise ProtocolError("invalid_permission_rule", str(exc), request_id=request.id) from exc
        return {"status": "revoked" if removed else "not_found", "rule_id": rule_id}

    def _permission_evaluate(self, request: ProtocolRequest) -> dict[str, Any]:
        tool_name = _required_str(request.params, "tool_name", request_id=request.id)
        raw_paths = request.params.get("paths", [])
        if not isinstance(raw_paths, list) or any(not isinstance(path, str) for path in raw_paths):
            raise ProtocolError(
                "invalid_field", "paths must be an array of strings.", request_id=request.id
            )
        paths = [path.strip() for path in raw_paths if path.strip()]
        command = _optional_str(request.params, "command", request_id=request.id)
        workspace = _optional_str(request.params, "workspace", request_id=request.id)
        sensitive, external_directory = _permission_path_safety_flags(paths, workspace)
        try:
            evaluation = self._permission_policy_store.evaluate(
                PermissionRequest.create(
                    tool_name,
                    paths=paths,
                    command=command,
                    workspace_root=workspace,
                    sensitive=sensitive,
                    external_directory=external_directory,
                )
            )
        except PermissionPolicyError as exc:
            raise ProtocolError(
                "invalid_permission_request", str(exc), request_id=request.id
            ) from exc
        return _permission_evaluation_payload(evaluation)

    def _permission_session_list(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        with session.approval_lock:
            grants = [
                {
                    "id": grant.grant_id,
                    "kind": grant.kind,
                    "scope_type": _scope_type(grant.scope),
                    "source": "session",
                }
                for grant in session.approved_approval_scopes
            ]
        return {"session_id": session.session_id, "grants": grants}

    def _permission_session_revoke(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        grant_id = _required_str(request.params, "grant_id", request_id=request.id)
        removed = False
        with session.approval_lock:
            for index, grant in enumerate(session.approved_approval_scopes):
                if grant.grant_id == grant_id:
                    del session.approved_approval_scopes[index]
                    removed = True
                    break
        return {
            "session_id": session.session_id,
            "grant_id": grant_id,
            "status": "revoked" if removed else "not_found",
        }

    def _job_status(self, request: ProtocolRequest) -> dict[str, Any]:
        job_id = _required_str(request.params, "job_id", request_id=request.id)
        with self._state_lock:
            job = self._jobs.get(job_id)
        if job is None or job.kind == "forge_plan":
            try:
                durable = self._forge_request_ledger.get(job_id)
            except ForgeRequestLedgerError as exc:
                raise ProtocolError(
                    "forge_plan_durability_error", str(exc), request_id=request.id
                ) from exc
            if durable is not None:
                with self._state_lock:
                    session = self._sessions.get(durable.session_id)
                    if session is not None and not session.closed:
                        job = self._attach_durable_forge_job_locked(session, durable)
                return _durable_forge_job_summary(durable, memory_job=job)
        if job is None:
            raise ProtocolError("job_not_found", "Job was not found.", request_id=request.id)
        return _job_summary(job)

    def _request_computer_use(
        self, session: BridgeSession, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        host = session.computer_use_host
        if (
            tool_name == "computer_verify"
            and host is not None
            and host.upload_objective is not None
        ):
            # A terminal-objective result belongs to this exact configured
            # session, route and job, not a replacement installed during a wait.
            agent, job, destination = (
                session.agent_session,
                session.active_job,
                session.computer_use_destination,
            )
            client, registry, cfg = agent.client, agent.model_registry, agent.cfg

            def route_fields() -> tuple[Any, ...]:
                return (
                    client.model,
                    client.base_url,
                    agent.api_key_source,
                    agent.api_key,
                    getattr(getattr(client, "route_identity", None), "credential_scope", None),
                    get_active_profile(cfg).name,
                )

            original_route = route_fields()

            def current() -> None:
                def owned() -> None:
                    if (
                        session.closed
                        or session.computer_use_host is not host
                        or session.agent_session is not agent
                        or session.active_job is not job
                        or session.computer_use_destination is not destination
                        or agent.client is not client
                        or agent.model_registry is not registry
                        or agent.cfg is not cfg
                        or route_fields() != original_route
                        or job is None
                        or not _job_is_active(job)
                        or job.cancellation_event.is_set()
                    ):
                        raise BridgeCancellationError(
                            getattr(job, "cancellation_reason", "")
                            or "computer_use_authority_changed"
                        )

                owned()
                _assert_live_computer_use_destination(session, request_id=None)
                owned()

            current()
            result = self._exchange_computer_use(session, tool_name, arguments)
            current()
            return result
        if tool_name not in {"computer_action", "computer_download", "computer_upload"}:
            return self._exchange_computer_use(session, tool_name, arguments)
        original = _json_clone(arguments)
        # Protocol authority can never originate in provider tool arguments.
        original.pop("approval_phase", None)
        original.pop("approval_token", None)
        preflight = self._exchange_computer_use(
            session, tool_name, {**original, "approval_phase": "prepare"}
        )
        if "error" in preflight:
            return preflight
        if tool_name == "computer_download" and "file_result" in preflight:
            if validate_file_result(preflight) and preflight["file_result"]["status"] in {
                "needs_user_setup",
                "permission_required",
            }:
                return preflight
            return {
                "error": "invalid_file_response",
                "message": "Download preparation cannot return a dispatched transfer.",
            }
        if tool_name == "computer_upload" and "upload_result" in preflight:
            projection = upload_result_projection(preflight)
            if (
                projection is not None
                and projection["upload_result"]["status"] == "needs_user_setup"
            ):
                return preflight
            return {
                "error": "invalid_upload_response",
                "message": "Upload preparation cannot return a dispatched transfer.",
            }
        challenge = preflight.get("approval_preflight")
        if (
            not isinstance(challenge, dict)
            or challenge.get("version") != 1
            or type(challenge.get("required")) is not bool
            or not re.fullmatch(r"[a-f0-9]{64}", str(challenge.get("token", "")))
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(challenge.get("scope_digest", "")))
        ):
            return {
                "error": "approval_preflight_required",
                "message": "The host did not prepare a bound action approval.",
            }
        job = session.active_job
        host = session.computer_use_host
        if job is None or host is None or job.cancellation_event.is_set():
            raise BridgeCancellationError(
                getattr(job, "cancellation_reason", "") or "computer_use_authority_changed"
            )
        host_received_at = time.monotonic()
        host_expires_at = _computer_use_approval_expiry(challenge.get("expires_at"))
        if host_expires_at is None:
            return {
                "error": "approval_preflight_required",
                "message": "The host did not provide a valid bounded approval expiry.",
            }
        dispatch_deadline = host_received_at + max(
            0.0,
            min(
                MAX_COMPUTER_USE_APPROVAL_SECONDS,
                (host_expires_at - datetime.now(UTC)).total_seconds(),
            ),
        )

        def approval_expired() -> bool:
            return datetime.now(UTC) >= host_expires_at or time.monotonic() >= dispatch_deadline

        if approval_expired():
            return {
                "error": "computer_use_approval_expired",
                "message": "The action approval expired; the action was not dispatched.",
                "dispatch_state": "not_dispatched",
            }
        # The engine independently treats every Return/Enter as potentially executable.
        keys = original.get("keys", [])
        if session.mode == "readonly":
            return {
                "error": "approval_denied_by_policy",
                "message": "Read only does not authorize control actions.",
                "dispatch_state": "not_dispatched",
            }
        needs_approval = (
            tool_name in {"computer_action", "computer_act", "computer_upload", "computer_download"}
            or challenge["required"]
            or (
                original.get("action") == "key"
                and isinstance(keys, list)
                and any(str(key).lower() in {"return", "enter"} for key in keys)
            )
        )
        if needs_approval:
            turn_arguments = {
                key: value
                for key, value in original.items()
                if key not in {"snapshot_id", "element_id"}
            }
            arguments_digest = hashlib.sha256(
                json.dumps(
                    turn_arguments, sort_keys=True, ensure_ascii=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            scope = {
                "type": "computer_use_turn_action",
                "job_id": job.job_id,
                "operation_id": host.operation_id,
                "scope_digest": challenge["scope_digest"],
                "arguments_digest": arguments_digest,
            }
            preview = str(challenge.get("preview", "Computer Use action"))[:2000]
            action_text = str(challenge.get("action_text", ""))[:10000]
            if action_text:
                preview += "\n\nExact text:\n" + action_text
            preview += "\n\nTurn permission covers only this exact action and target in this turn."
            approval = ApprovalRequest(
                kind="computer_use",
                reason=str(challenge.get("reason", "Approval required"))[:2000],
                preview=preview,
                command=None,
                allow_for_session_scope=scope,
                metadata={
                    "operation_id": host.operation_id,
                    "arguments_digest": hashlib.sha256(
                        json.dumps(
                            original, sort_keys=True, ensure_ascii=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                    "tool_call_id": session.surface.current_tool_call_id() or "",
                    "scope_digest": challenge["scope_digest"],
                    "approval_scope": "turn",
                },
            )

            def emit_transient(event: Any) -> None:
                envelope = session.surface._sequencer.envelope(
                    event, context=session.surface.context
                )
                self._write_transient_computer_use_request(envelope)

            dispatch_deadline = min(
                dispatch_deadline,
                time.monotonic()
                + min(self._approval_timeout_seconds, MAX_COMPUTER_USE_APPROVAL_SECONDS),
            )

            class BoundActionSurface:
                def standing_approval(_self, request):
                    return self._standing_approval(session, request)

                def request_approval(_self, request):
                    return self._request_approval(
                        session,
                        request,
                        emit_transient,
                        host_expires_at=host_expires_at,
                        host_deadline=dispatch_deadline,
                    )

            decision = decide_action_approval(
                approval,
                mode=session.mode,
                root=session.root,
                surface=BoundActionSurface(),
                can_ask_person=True,
                expiry_as_error=False,
            )
            if job is not session.active_job or job.cancellation_event.is_set() or session.closed:
                raise BridgeCancellationError(
                    getattr(job, "cancellation_reason", "") or "computer_use_authority_changed"
                )
            if approval_expired():
                return {
                    "error": "computer_use_approval_expired",
                    "message": "The action approval expired; the action was not dispatched.",
                    "dispatch_state": "not_dispatched",
                }
            if not decision.allow:
                return {
                    "error": "approval_denied_by_policy"
                    if decision.status == "denied_by_policy"
                    else "computer_use_approval_" + (decision.status or "denied"),
                    "message": "The action was not dispatched: approval "
                    + (decision.status or "denied")
                    + ".",
                    "dispatch_state": "not_dispatched",
                }
        if job is not session.active_job or job.cancellation_event.is_set() or session.closed:
            raise BridgeCancellationError(
                getattr(job, "cancellation_reason", "") or "computer_use_authority_changed"
            )
        # Even a cached turn permission cannot revive this particular challenge.
        if approval_expired():
            return {
                "error": "computer_use_approval_expired",
                "message": "The action approval expired; the action was not dispatched.",
                "dispatch_state": "not_dispatched",
            }
        return self._exchange_computer_use(
            session,
            tool_name,
            {**original, "approval_phase": "execute", "approval_token": challenge["token"]},
        )

    def _task_budget_context(self, session: BridgeSession, job: BridgeJob) -> Any:
        host = session.computer_use_host
        if host is None or host.task_budget is None:
            return nullcontext()
        budget_need(not session.task_budget_consumed)
        session.task_budget_consumed = True
        agent = session.agent_session
        _assert_task_budget_client_supported(agent)
        client = agent.client
        registry = agent.model_registry
        pinned = {
            key: _json_clone(getattr(client, key, None))
            for key in ("model", "base_url", "api_key", "extra_headers")
        }
        pinned_route, pinned_auth = client.route_identity, getattr(client, "provider_auth", None)

        def current() -> str:
            budget_need(
                not session.closed
                and session.agent_session is agent
                and agent.client is client
                and agent.model_registry is registry
                and session.active_job is job
                and not job.cancellation_event.is_set()
                and _job_is_active(job)
            )
            _assert_task_budget_client_supported(agent)
            budget_need(
                client.route_identity == pinned_route
                and getattr(client, "provider_auth", None) is pinned_auth
                and all(getattr(client, key, None) == value for key, value in pinned.items())
            )
            _assert_live_computer_use_destination(session, request_id=None)
            budget_need(
                not session.closed
                and session.active_job is job
                and not job.cancellation_event.is_set()
            )
            return job.job_id

        def emit(payload: dict[str, Any]) -> None:
            current()
            # The joined HTTP worker does not inherit threading.local surface
            # context. Bind the captured actual job, never a provider field.
            previous = session.surface.context.job_id
            session.surface.with_job(job.job_id)
            try:
                self._write_transient_computer_use_request(
                    session.surface.transient_envelope(
                        ProtocolPayloadEvent("task_budget_request", payload)
                    )
                )
            finally:
                session.surface.with_job(previous)

        channel = TaskBudgetChannel(
            host.task_budget, host.operation_id, emit=emit, assert_current=current
        )
        authority = TaskBudgetAuthority(
            host.task_budget,
            exchange=channel.exchange,
            assert_current=current,
            cancelled=lambda: job.cancellation_event.is_set() or session.closed,
        )
        session.task_budget_channel, session.task_budget_authority = channel, authority
        agent.execution_deadline = authority.execution_deadline(agent.execution_deadline)
        return authority.activate()

    def _task_budget_respond(
        self, request: ProtocolRequest
    ) -> tuple[dict[str, Any], Callable[[], None]]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        try:
            budget_need(session.task_budget_channel is not None)
            return session.task_budget_channel.respond(request.params, session.session_id)
        except TaskBudgetRefused:
            raise ProtocolError(
                "task_budget_refused",
                "The exact private task budget request is no longer available.",
                request_id=request.id,
            ) from None

    def _request_browser_start(self, session: BridgeSession, url: Any) -> Any:
        selection = url if isinstance(url, dict) else None
        if selection is not None:
            if not valid_computer_selection(
                selection, (session.browser_start_host or {}).get("discovery")
            ):
                return {"error": "computer_start_invalid_selection"}
        elif not valid_browser_start_url(url):
            return {"error": "browser_start_invalid_url"}
        with self._state_lock:
            host, job = session.browser_start_host, session.active_job
            call_id = session.surface.current_tool_call_id()
            if (
                host is None
                or session.closed
                or session.sensitive_media
                or session.browser_start_consumed
                or session.pending_browser_start is not None
                or job is None
                or not _job_is_active(job)
                or session.surface.context.job_id != job.job_id
                or not call_id
            ):
                return {"error": "browser_start_unavailable"}
            _check_job_cancelled(job)
            identities = {
                "session_id": session.session_id,
                "job_id": job.job_id,
                "tool_call_id": call_id,
                "operation_id": host["operation_id"],
                "host_nonce": host["host_nonce"],
                "request_id": str(uuid.uuid4()),
            }
            pending = {
                "identities": identities,
                "done": threading.Event(),
                "deadline": time.monotonic() + 30.0,
                "response": None,
            }
            session.pending_browser_start = pending
            payload = {
                key: value
                for key, value in identities.items()
                if key not in {"session_id", "job_id"}
            }
            self._write_transient_computer_use_request(
                session.surface.transient_envelope(
                    ProtocolPayloadEvent(
                        "browser_start_request",
                        {
                            **payload,
                            **({"selection": selection} if selection is not None else {"url": url}),
                        },
                    )
                )
            )
        try:
            while not pending["done"].wait(0.05):
                with self._state_lock:
                    _check_job_cancelled(job)
                    if session.closed or session.active_job is not job:
                        raise BridgeCancellationError("cancelled_by_user")
                    if time.monotonic() >= pending["deadline"]:
                        return {"error": "browser_start_timeout"}
            with self._state_lock:
                _check_job_cancelled(job)
                if session.closed or session.active_job is not job:
                    raise BridgeCancellationError("cancelled_by_user")
                response = pending["response"]
                if not response["allow"]:
                    return {"error": response.get("code") or "browser_start_refused"}
                session.browser_start_consumed = True
                job.browser_start_handoff = {
                    key: identities[key] for key in ("request_id", "operation_id", "host_nonce")
                }
                return BrowserStartHandoff(identities["request_id"])
        finally:
            with self._state_lock:
                if session.pending_browser_start is pending:
                    session.pending_browser_start = None

    def _browser_start_respond(self, request: ProtocolRequest) -> dict[str, Any]:
        params = request.params
        with self._state_lock:
            session = (
                self._sessions.get(params.get("session_id"))
                if isinstance(params.get("session_id"), str)
                else None
            )
            pending = session.pending_browser_start if session is not None else None
            job = session.active_job if session is not None else None
            identity_keys = {
                "session_id",
                "job_id",
                "tool_call_id",
                "operation_id",
                "host_nonce",
                "request_id",
            }
            valid = (
                pending is not None
                and session is not None
                and not session.closed
                and job is not None
                and _job_is_active(job)
                and not job.cancellation_event.is_set()
                and not pending["done"].is_set()
                and time.monotonic() < pending["deadline"]
                and set(params) in (identity_keys | {"allow"}, identity_keys | {"allow", "code"})
                and type(params.get("allow")) is bool
                and all(params.get(key) == value for key, value in pending["identities"].items())
                and job.job_id == pending["identities"]["job_id"]
                and (
                    "code" not in params
                    or isinstance(params["code"], str)
                    and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", params["code"]) is not None
                )
            )
            if not valid:
                raise ProtocolError(
                    "browser_start_response_refused",
                    "The exact browser start request is no longer available.",
                    request_id=request.id,
                )
            pending["response"] = {
                "allow": params["allow"],
                **({"code": params["code"]} if "code" in params else {}),
            }
            pending["done"].set()
            return {"status": "accepted", "request_id": params["request_id"]}

    def _exchange_computer_use(
        self,
        session: BridgeSession,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        host = session.computer_use_host
        if host is None or tool_name not in COMPUTER_USE_TOOL_NAMES + (
            COMPUTER_USE_VERIFICATION_TOOL_NAMES if host.verification_available else ()
        ) + (COMPUTER_USE_FILE_TOOL_NAMES if host.files_available else ()) + (
            COMPUTER_USE_UPLOAD_TOOL_NAMES if host.uploads_available else ()
        ):
            return {
                "error": "computer_use_host_unavailable",
                "message": "The trusted Computer Use host is unavailable for this session.",
            }
        context = session.surface.context
        job_id = str(context.job_id or "").strip()
        current_tool_call_id = getattr(session.surface, "current_tool_call_id", None)
        tool_call_id = (
            str(current_tool_call_id() or "").strip() if callable(current_tool_call_id) else ""
        )
        if not job_id or not tool_call_id:
            return {
                "error": "computer_use_binding_missing",
                "message": "The Computer Use request is missing its job or tool-call binding.",
            }
        with self._state_lock:
            active_job = session.active_job
        if active_job is None or active_job.job_id != job_id or not _job_is_active(active_job):
            return {
                "error": "computer_use_job_inactive",
                "message": "The Computer Use job is no longer active.",
            }
        if active_job.cancellation_event.is_set():
            raise BridgeCancellationError("cancelled_by_user")

        budget = session.task_budget_authority
        if host.task_budget is not None:
            budget_need(budget is not None)
            budget.ensure_lease()
        clean_arguments = _json_clone(arguments)
        arguments_fingerprint = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    clean_arguments,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
        )
        request_id = f"cu_{uuid.uuid4().hex}"
        response_nonce = uuid.uuid4().hex + uuid.uuid4().hex
        expires_at = _expires_at(DEFAULT_COMPUTER_USE_TIMEOUT_SECONDS)
        if budget is not None:
            expires_at = budget_iso(
                min(budget_stamp(expires_at), budget_stamp(budget.binding["leaseExpiresAt"]))
            )
        pending = PendingComputerUseRequest(
            request_id=request_id,
            response_nonce=response_nonce,
            session_id=session.session_id,
            operation_id=host.operation_id,
            job_id=job_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments_fingerprint=arguments_fingerprint,
            expires_at=expires_at,
            deadline=time.monotonic() + max(0, budget_stamp(expires_at) - time.time()),
            approval_preflight_expected=(
                tool_name in {"computer_action", "computer_download", "computer_upload"}
                and clean_arguments.get("approval_phase") == "prepare"
            ),
        )
        # Serialize the final cancellation check with session.cancel's event set.
        # A request emitted while this lock is held is ordered before Stop; once
        # Stop has acquired the lock, no later host request can be registered.
        with self._state_lock:
            active_job = session.active_job
            if (
                active_job is None
                or active_job.job_id != job_id
                or not _job_is_active(active_job)
                or active_job.cancellation_event.is_set()
            ):
                raise BridgeCancellationError("cancelled_by_user")
            with session.computer_use_lock:
                session.pending_computer_use[request_id] = pending

            envelope = session.surface.transient_envelope(
                ProtocolPayloadEvent(
                    "computer_use_request",
                    {
                        "protocol_version": 1,
                        "request_id": request_id,
                        "response_nonce": response_nonce,
                        "operation_id": host.operation_id,
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "arguments": clean_arguments,
                        "arguments_fingerprint": arguments_fingerprint,
                        "broker_fingerprint": host.broker_fingerprint,
                        "authority_epoch": host.authority_epoch,
                        "scope_digest": host.scope_digest,
                        "expires_at": expires_at,
                        **(
                            {"budget_lease_id": budget.binding["leaseId"]}
                            if budget is not None
                            else {}
                        ),
                    },
                )
            )
            # Deliberately bypass _record_and_write_event: arguments may contain typed text or
            # Accessibility labels and are never eligible for replay, trace, or persistence.
            self._write_transient_computer_use_request(envelope)

        try:
            while True:
                remaining = pending.deadline - time.monotonic()
                if remaining <= 0:
                    with session.computer_use_lock:
                        if not pending.resolved:
                            pending.response = {
                                "error": "computer_use_timeout",
                                "message": "The Computer Use host did not respond before expiry.",
                            }
                            pending.resolved = True
                            pending.done.set()
                    break
                if pending.done.wait(timeout=min(0.05, remaining)):
                    break
                if session.closed or active_job.cancellation_event.is_set():
                    self._cancel_pending_computer_use(session, "cancelled_by_user")
                    raise BridgeCancellationError("cancelled_by_user")
            # session.cancel sets the job event before it resolves pending host
            # requests.  Recheck after the wait wakes so its synthetic host error
            # cannot flow back through the ordinary tool/final-failure path after
            # Stop has already been acknowledged.
            if session.closed:
                raise BridgeCancellationError("cancelled_by_user")
            _check_job_cancelled(active_job)
            response = pending.response or {
                "error": "invalid_host_response",
                "message": "The Computer Use host returned no response.",
            }
        finally:
            with session.computer_use_lock:
                session.pending_computer_use.pop(request_id, None)
                session.resolved_computer_use_ids.add(request_id)
                while len(session.resolved_computer_use_ids) > 512:
                    session.resolved_computer_use_ids.pop()

        if budget is not None and response.get("error") == "computer_use_lease_renewal_required":
            # A validated host response refused this call before dispatch. The
            # short mutation allowance can end before its wall-clock expiry.
            # Renew only through the independent host verifier; never replay the
            # refused action or an approval from the retired lease.
            return budget.renew_after_host_refusal()
        return response

    def _computer_respond(self, request: ProtocolRequest) -> dict[str, Any]:
        params = request.params
        session_id = _required_str(params, "session_id", request_id=request.id)
        request_key = _required_str(params, "request_id", request_id=request.id)
        session = self._require_session(session_id, request_id=request.id)
        host = session.computer_use_host
        if host is None:
            raise ProtocolError(
                "computer_use_host_unavailable",
                "The session has no trusted Computer Use host binding.",
                request_id=request.id,
            )
        with session.computer_use_lock:
            pending = session.pending_computer_use.get(request_key)
            if pending is None:
                code = (
                    "duplicate_response"
                    if request_key in session.resolved_computer_use_ids
                    else "unknown_computer_request"
                )
                raise ProtocolError(
                    code,
                    "No unresolved Computer Use request exists with that id.",
                    request_id=request.id,
                )
            if pending.resolved:
                raise ProtocolError(
                    "duplicate_response",
                    "The Computer Use request was already resolved.",
                    request_id=request.id,
                )
            expected_strings = {
                "response_nonce": pending.response_nonce,
                "operation_id": pending.operation_id,
                "job_id": pending.job_id,
                "tool_call_id": pending.tool_call_id,
                "tool_name": pending.tool_name,
                "arguments_fingerprint": pending.arguments_fingerprint,
                "expires_at": pending.expires_at,
            }
            for field_name, expected in expected_strings.items():
                supplied = _required_str(params, field_name, request_id=request.id)
                if not hmac.compare_digest(expected, supplied):
                    raise ProtocolError(
                        "computer_use_binding_mismatch",
                        f"Computer Use response {field_name} does not match the pending request.",
                        request_id=request.id,
                    )
            if time.monotonic() >= pending.deadline:
                pending.response = {
                    "error": "computer_use_timeout",
                    "message": "The Computer Use host response arrived after expiry.",
                }
                pending.resolved = True
                pending.done.set()
                raise ProtocolError(
                    "expired_computer_request",
                    "The Computer Use request has expired.",
                    request_id=request.id,
                )
            ok = _approval_required_bool(params, "ok", request_id=request.id)
            _reject_computer_response_inline_secrets(
                params, pending=pending, ok=ok, request_id=request.id
            )
            if ok:
                raw_result = params.get("result")
                if not isinstance(raw_result, dict):
                    raise ProtocolError(
                        "invalid_computer_response",
                        "A successful Computer Use response requires an object result.",
                        request_id=request.id,
                    )
                response = _json_clone(raw_result)
            else:
                raw_error = params.get("error")
                if not isinstance(raw_error, dict):
                    raise ProtocolError(
                        "invalid_computer_response",
                        "A failed Computer Use response requires an error object.",
                        request_id=request.id,
                    )
                code = str(raw_error.get("code") or "computer_use_failed").strip()
                message = str(
                    raw_error.get("message") or "Computer Use host request failed."
                ).strip()
                response = {
                    "error": code[:128] or "computer_use_failed",
                    "message": message[:2000] or "Computer Use host request failed.",
                }
            pending.response = response
            pending.resolved = True
            pending.done.set()
        return {
            "session_id": session.session_id,
            "request_id": request_key,
            "status": "resolved",
            "ok": ok,
        }

    def _approval_respond(self, request: ProtocolRequest) -> dict[str, Any]:
        session_id = _approval_required_str(request.params, "session_id", request_id=request.id)
        approval_id = _approval_required_str(request.params, "approval_id", request_id=request.id)
        allow = _approval_required_bool(request.params, "allow", request_id=request.id)
        allow_for_session = _approval_required_bool(
            request.params,
            "allow_for_session",
            request_id=request.id,
        )
        session = self._require_session_for_approval(session_id, request_id=request.id)
        return self._resolve_approval(
            session=session,
            approval_id=approval_id,
            allow=allow,
            allow_for_session=allow_for_session,
            response_bindings=request.params,
            request_id=request.id,
        )

    def _session_list(self, request: ProtocolRequest) -> dict[str, Any]:
        _ = request
        with self._state_lock:
            for session in self._sessions.values():
                _reconcile_session_job_state(session)
            sessions = [_session_summary(session) for session in self._sessions.values()]
        return {"sessions": sessions}

    def _session_get_events(self, request: ProtocolRequest) -> dict[str, Any]:
        session_id = _required_str(request.params, "session_id", request_id=request.id)
        _validate_session_id(session_id, request_id=request.id)
        max_events = _bounded_int(
            request.params.get("max_events"),
            default=EVENT_REPLAY_RESPONSE_MAX,
            upper=EVENT_REPLAY_RESPONSE_MAX,
            request_id=request.id,
        )
        after_sequence = _optional_int(
            request.params.get("after_sequence"),
            request_id=request.id,
        )
        with self._state_lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ProtocolError(
                    "session_not_found", "Session was not found.", request_id=request.id
                )
            retained = list(self._event_buffers.get(session_id, ()))
            dropped_count = int(self._event_dropped_counts.get(session_id, 0))
        if not retained:
            return {
                "session_id": session_id,
                "events": [],
                "truncated": False,
                "lowest_retained_sequence": None,
                "highest_retained_sequence": None,
                "max_events": max_events,
                "dropped_event_count": dropped_count,
            }
        lowest = int(retained[0]["sequence"])
        highest = int(retained[-1]["sequence"])
        if after_sequence is None:
            selected = retained[-max_events:]
            truncated = len(retained) > len(selected)
        else:
            selected_all = [event for event in retained if int(event["sequence"]) > after_sequence]
            selected = selected_all[:max_events]
            truncated = after_sequence < lowest - 1 or len(selected_all) > len(selected)
        if session.sensitive_media:
            selected = [_redact_sensitive_media_projection(session, event) for event in selected]
        return {
            "session_id": session_id,
            "events": selected,
            "truncated": truncated,
            "lowest_retained_sequence": lowest,
            "highest_retained_sequence": highest,
            "max_events": max_events,
            "dropped_event_count": dropped_count,
        }

    def _artifact_list(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        try:
            payload = session.artifact_store.list(
                max_items=request.params.get("max_items"),
                max_depth=request.params.get("max_depth"),
            )
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        payload["session_id"] = session.session_id
        return payload

    def _artifact_read(self, request: ProtocolRequest) -> dict[str, Any]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        artifact_id = _required_str(request.params, "artifact_id", request_id=request.id)
        try:
            payload = session.artifact_store.read(
                artifact_id,
                max_bytes=request.params.get("max_bytes"),
            )
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        payload["session_id"] = session.session_id
        return payload

    def _browser_context(
        self, request: ProtocolRequest
    ) -> tuple[BridgeSession, ManagedBrowserService]:
        session = self._require_session(
            _required_str(request.params, "session_id", request_id=request.id),
            request_id=request.id,
        )
        return session, self._managed_browser_for_session(session, request_id=request.id)

    def _browser_start(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        session, browser = self._browser_context(request)
        legacy_allow_local = _optional_bool(
            request.params,
            "allow_local_destinations",
            default=False,
            request_id=request.id,
        )
        raw_scope = _optional_str(request.params, "network_scope", request_id=request.id)
        scope = raw_scope or ("local_network" if legacy_allow_local else "public")
        if scope not in {"public", "public_loopback", "local_network"}:
            raise ProtocolError(
                "invalid_field",
                "network_scope must be public or public_loopback.",
                request_id=request.id,
            )
        if raw_scope is not None and legacy_allow_local:
            raise ProtocolError(
                "invalid_field",
                "Use network_scope instead of combining it with allow_local_destinations.",
                request_id=request.id,
            )
        allow_local = scope != "public"
        loopback_only = scope == "public_loopback"
        if allow_local:
            self._require_confirmation(request)
        try:
            status = browser.start(
                session.session_id,
                executable_path=_optional_str(
                    request.params, "executable_path", request_id=request.id
                ),
                allow_local_destinations=allow_local,
                local_destinations_loopback_only=loopback_only,
            )
        except BrowserError as exc:
            raise ProtocolError("browser_error", str(exc), request_id=request.id) from exc
        return {"session_id": session.session_id, **_browser_status_payload(status)}

    def _browser_navigate(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        session, browser = self._browser_context(request)
        try:
            payload = browser.navigate(
                session.session_id,
                _required_str(request.params, "browser_session_id", request_id=request.id),
                _required_str(request.params, "url", request_id=request.id),
                timeout=_optional_browser_timeout(request.params, request_id=request.id),
            )
        except BrowserError as exc:
            raise ProtocolError("browser_error", str(exc), request_id=request.id) from exc
        return _browser_protocol_payload(session.session_id, payload)

    def _browser_snapshot(self, request: ProtocolRequest) -> dict[str, Any]:
        session, browser = self._browser_context(request)
        try:
            payload = browser.snapshot(
                session.session_id,
                _required_str(request.params, "browser_session_id", request_id=request.id),
                kind=_optional_str(request.params, "kind", request_id=request.id) or "semantic",
                timeout=_optional_browser_timeout(request.params, request_id=request.id),
            )
        except BrowserError as exc:
            raise ProtocolError("browser_error", str(exc), request_id=request.id) from exc
        return _browser_protocol_payload(session.session_id, payload)

    def _browser_screenshot(self, request: ProtocolRequest) -> dict[str, Any]:
        session, browser = self._browser_context(request)
        try:
            artifact = browser.screenshot(
                session.session_id,
                _required_str(request.params, "browser_session_id", request_id=request.id),
                full_page=_optional_bool(
                    request.params, "full_page", default=False, request_id=request.id
                ),
                timeout=_optional_browser_timeout(request.params, request_id=request.id),
            )
        except BrowserError as exc:
            raise ProtocolError("browser_error", str(exc), request_id=request.id) from exc
        return {"session_id": session.session_id, **_browser_artifact_payload(artifact)}

    def _browser_artifact_read(self, request: ProtocolRequest) -> dict[str, Any]:
        session, browser = self._browser_context(request)
        browser_session_id = _required_str(
            request.params, "browser_session_id", request_id=request.id
        )
        try:
            payload = browser.read_artifact(
                session.session_id,
                browser_session_id,
                _required_str(request.params, "artifact_id", request_id=request.id),
                offset=_non_negative_int_param(
                    request.params, "offset", default=0, request_id=request.id
                ),
                max_bytes=_positive_int_param(
                    request.params,
                    "max_bytes",
                    default=256 * 1024,
                    upper=1024 * 1024,
                    request_id=request.id,
                ),
            )
        except BrowserError as exc:
            raise ProtocolError("browser_error", str(exc), request_id=request.id) from exc
        return {
            "session_id": session.session_id,
            "browser_session_id": browser_session_id,
            **payload,
        }

    def _browser_diagnostics(self, request: ProtocolRequest) -> dict[str, Any]:
        session, browser = self._browser_context(request)
        max_events = (
            None
            if request.params.get("max_events") is None
            else _positive_int_param(
                request.params,
                "max_events",
                default=100,
                upper=500,
                request_id=request.id,
            )
        )
        try:
            payload = browser.diagnostics(
                session.session_id,
                _required_str(request.params, "browser_session_id", request_id=request.id),
                max_events=max_events,
                timeout=_optional_browser_timeout(request.params, request_id=request.id),
            )
        except BrowserError as exc:
            raise ProtocolError("browser_error", str(exc), request_id=request.id) from exc
        return _browser_protocol_payload(session.session_id, payload)

    def _browser_click(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        session, browser = self._browser_context(request)
        try:
            payload = browser.click(
                session.session_id,
                _required_str(request.params, "browser_session_id", request_id=request.id),
                _required_str(request.params, "selector", request_id=request.id),
                timeout=_optional_browser_timeout(request.params, request_id=request.id),
            )
        except BrowserError as exc:
            raise ProtocolError("browser_error", str(exc), request_id=request.id) from exc
        return _browser_protocol_payload(session.session_id, payload)

    def _browser_type(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        session, browser = self._browser_context(request)
        try:
            payload = browser.type_text(
                session.session_id,
                _required_str(request.params, "browser_session_id", request_id=request.id),
                _required_str(request.params, "selector", request_id=request.id),
                _required_str(request.params, "text", request_id=request.id),
                replace=_optional_bool(
                    request.params, "replace", default=True, request_id=request.id
                ),
                timeout=_optional_browser_timeout(request.params, request_id=request.id),
            )
        except BrowserError as exc:
            raise ProtocolError("browser_error", str(exc), request_id=request.id) from exc
        return _browser_protocol_payload(session.session_id, payload)

    def _browser_status(self, request: ProtocolRequest) -> dict[str, Any]:
        session, browser = self._browser_context(request)
        try:
            status = browser.status(
                session.session_id,
                _required_str(request.params, "browser_session_id", request_id=request.id),
            )
        except BrowserError as exc:
            raise ProtocolError("browser_error", str(exc), request_id=request.id) from exc
        return {"session_id": session.session_id, **_browser_status_payload(status)}

    def _browser_list(self, request: ProtocolRequest) -> dict[str, Any]:
        session, browser = self._browser_context(request)
        try:
            statuses = browser.list(session.session_id)
        except BrowserError as exc:
            raise ProtocolError("browser_error", str(exc), request_id=request.id) from exc
        items = [_browser_status_payload(status) for status in statuses]
        return {"session_id": session.session_id, "browsers": items, "count": len(items)}

    def _browser_close(self, request: ProtocolRequest) -> dict[str, Any]:
        session, browser = self._browser_context(request)
        delete_artifacts = _optional_bool(
            request.params, "delete_artifacts", default=True, request_id=request.id
        )
        if not delete_artifacts:
            raise ProtocolError(
                "unsupported_artifact_retention",
                "Closed browser screenshots cannot be retained because they would no longer have an owned read lifecycle.",
                request_id=request.id,
            )
        self._require_confirmation(request)
        browser_session_id = _required_str(
            request.params, "browser_session_id", request_id=request.id
        )
        try:
            closed = browser.close(
                session.session_id,
                browser_session_id,
                delete_artifacts=delete_artifacts,
            )
        except BrowserError as exc:
            raise ProtocolError("browser_error", str(exc), request_id=request.id) from exc
        return {
            "session_id": session.session_id,
            "browser_session_id": browser_session_id,
            "status": "closed" if closed else "not_found",
        }

    def _forge_plan(self, request: ProtocolRequest) -> dict[str, Any]:
        params = request.params
        instruction = _required_str(params, "instruction", request_id=request.id)
        created_session = False
        session_id = str(params.get("session_id") or "").strip()
        if session_id:
            session = self._require_session(session_id, request_id=request.id)
        else:
            created = self._session_create(
                ProtocolRequest(
                    id=request.id,
                    method="session.create",
                    params=params,
                    protocol_version=request.protocol_version,
                )
            )
            session = self._require_session(
                str(created["session_id"]),
                request_id=request.id,
            )
            created_session = True
        with self._state_lock:
            _reconcile_session_job_state(session)
            if _job_is_active(session.active_job):
                raise ProtocolError(
                    "session_busy",
                    "Session already has a running job.",
                    request_id=request.id,
                )
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, params, request_id=request.id)
        try:
            record, plan = create_ide_forge_plan(
                session_id=session.session_id,
                workspace_root=session.root,
                instruction=instruction,
                cfg=cfg,
            )
        except ProtocolError as e:
            if created_session:
                self._close_session(session)
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        result = forge_plan_result(record, plan, created_session=created_session)
        with self._state_lock:
            self._forge_plans[record.plan_id] = record
            session.artifact_store.add_root(
                ArtifactRoot(
                    forge_artifact_root_name(record.plan_id),
                    validate_forge_run_artifact_root(record),
                )
            )
        session.surface.emit_status_update(mode=session.mode)
        for task in result["tasks"]:
            session.surface.emit_plan_node_updated(
                str(task.get("task_id") or ""),
                str(task.get("status") or "planned"),
                str(task.get("title") or ""),
            )
        for warning in result.get("warnings") or []:
            session.surface.emit_warning(str(warning))
        session.surface.emit_info(f"forge_plan_created {record.plan_id}")
        return result

    def _forge_plan_start(
        self, request: ProtocolRequest
    ) -> tuple[dict[str, Any], Callable[[], None]]:
        params = dict(request.params)
        instruction = _required_str(params, "instruction", request_id=request.id)
        idempotency_key = _optional_str(params, "idempotency_key", request_id=request.id)
        if not idempotency_key:
            # Legacy clients did not send a durable key.  Preserve compatibility
            # without pretending that independently generated calls are retries.
            idempotency_key = f"legacy-{uuid.uuid4().hex}"
        created_session = False
        session_id = str(params.get("session_id") or "").strip()
        if session_id:
            session = self._require_session(session_id, request_id=request.id)
        else:
            created = self._session_create(
                ProtocolRequest(
                    id=request.id,
                    method="session.create",
                    params=params,
                    protocol_version=request.protocol_version,
                )
            )
            session = self._require_session(str(created["session_id"]), request_id=request.id)
            created_session = True

        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, params, request_id=request.id)
        try:
            accepted = self._forge_request_ledger.accept(
                workspace_root=session.root,
                session_id=session.session_id,
                idempotency_key=idempotency_key,
                payload=_forge_plan_request_fingerprint_payload(params),
            )
        except ForgeRequestIdempotencyConflict as exc:
            if created_session:
                self._close_session(session)
            raise ProtocolError(
                "idempotency_conflict",
                str(exc),
                request_id=request.id,
            ) from exc
        except ForgeRequestLedgerError as exc:
            if created_session:
                self._close_session(session)
            raise ProtocolError(
                "forge_plan_durability_error",
                str(exc),
                request_id=request.id,
            ) from exc

        with self._state_lock:
            _reconcile_session_job_state(session)
            active = session.active_job
            session_busy = bool(
                _job_is_active(active)
                and active is not None
                and active.job_id != accepted.record.job_id
            )
            job = (
                None
                if session_busy
                else self._attach_durable_forge_job_locked(session, accepted.record)
            )

        dispatch_lease = accepted.dispatch_lease
        if session_busy:
            if dispatch_lease is not None:
                with suppress(ForgeRequestLedgerError):
                    self._forge_request_ledger.reject(dispatch_lease, error_code="session_busy")
            if created_session:
                self._close_session(session)
            raise ProtocolError(
                "session_busy",
                "Session already has a running job.",
                request_id=request.id,
            )
        assert job is not None
        if (
            dispatch_lease is None
            and accepted.record.state in {ForgeRequestState.QUEUED, ForgeRequestState.RUNNING}
            and job.thread is None
        ):
            # Observation is not worker ownership: a duplicate bridge cannot
            # signal the other process's in-memory cancellation token.
            job.cancellable = False

        def _start() -> None:
            if dispatch_lease is None:
                return
            thread = threading.Thread(
                target=self._run_forge_plan_job,
                args=(
                    session,
                    job,
                    instruction,
                    cfg,
                    created_session,
                    dispatch_lease,
                ),
                name=f"alysis-ide-forge-plan-{job.job_id}",
                daemon=True,
            )
            self._start_job_thread(session, job, thread)

        return {
            "session_id": session.session_id,
            "job_id": job.job_id,
            "status": "started" if dispatch_lease is not None else job.status,
            "durably_accepted": True,
            "duplicate": not accepted.created,
        }, _start

    def _run_forge_plan_job(
        self,
        session: BridgeSession,
        job: BridgeJob,
        instruction: str,
        cfg: Any,
        created_session: bool,
        dispatch_lease: ForgeDispatchLease,
    ) -> None:
        session.surface.with_job(job.job_id)
        heartbeat_stop = threading.Event()
        heartbeat_lost = threading.Event()
        lease_lock = threading.Lock()
        lease_holder: list[ForgeWorkerLease] = []
        heartbeat_thread: threading.Thread | None = None

        def _current_lease() -> ForgeWorkerLease:
            with lease_lock:
                if not lease_holder:
                    raise ForgeRequestLeaseLost("Forge plan worker lease is unavailable.")
                return lease_holder[0]

        def _stop_heartbeat() -> None:
            heartbeat_stop.set()
            if heartbeat_thread is not None and heartbeat_thread is not threading.current_thread():
                heartbeat_thread.join(
                    timeout=max(1.0, self._forge_request_ledger.config.lease_seconds)
                )
            if heartbeat_lost.is_set():
                raise ForgeRequestLeaseLost("Forge plan worker lease was lost.")

        try:
            worker_lease = self._forge_request_ledger.begin(dispatch_lease)
            job.durable_forge_lease = worker_lease
            lease_holder.append(worker_lease)

            def _heartbeat() -> None:
                interval = max(
                    0.25,
                    min(10.0, self._forge_request_ledger.config.lease_seconds / 3.0),
                )
                renewal_deadline = (
                    time.monotonic() + self._forge_request_ledger.config.lease_seconds
                )
                while not heartbeat_stop.wait(interval):
                    try:
                        renewed = self._forge_request_ledger.renew(_current_lease())
                    except ForgeRequestLedgerError:
                        if time.monotonic() < renewal_deadline:
                            continue
                        heartbeat_lost.set()
                        job.cancellation_event.set()
                        return
                    with lease_lock:
                        lease_holder[0] = renewed
                        job.durable_forge_lease = renewed
                    renewal_deadline = (
                        time.monotonic() + self._forge_request_ledger.config.lease_seconds
                    )

            heartbeat_thread = threading.Thread(
                target=_heartbeat,
                name=f"alysis-ide-forge-plan-heartbeat-{job.job_id}",
                daemon=True,
            )
            heartbeat_thread.start()
            with self._state_lock:
                _mark_job_running(job)
            session.surface.emit_status_update(mode=session.mode, model=getattr(cfg, "model", None))
            session.surface.emit_info(f"forge_plan_started {job.job_id}")
            _check_job_cancelled(job)
            record, plan = create_ide_forge_plan(
                session_id=session.session_id,
                workspace_root=session.root,
                instruction=instruction,
                cfg=cfg,
                cancellation_token=BridgeCancellationToken(job.cancellation_event),
            )
            _check_job_cancelled(job)
            record = ForgePlanRecord(
                session_id=record.session_id,
                plan_id=record.plan_id,
                paths=record.paths,
                status="planned",
                job_id=job.job_id,
                source=record.source,
                warnings=record.warnings,
                verification_commands=record.verification_commands,
            )
            result = forge_plan_result(record, plan, created_session=created_session)
            _stop_heartbeat()
            self._forge_request_ledger.complete(_current_lease(), plan_id=record.plan_id)
            with self._state_lock:
                self._forge_plans[record.plan_id] = record
                session.artifact_store.add_root(
                    ArtifactRoot(
                        forge_artifact_root_name(record.plan_id),
                        validate_forge_run_artifact_root(record),
                    )
                )
                job.plan_id = record.plan_id
                _mark_job_completed(job, status="completed", result=result)
                _reconcile_session_job_state(session)
            session.surface.emit_status_update(mode=session.mode, model=getattr(cfg, "model", None))
            for task in result["tasks"]:
                session.surface.emit_plan_node_updated(
                    str(task.get("task_id") or ""),
                    str(task.get("status") or "planned"),
                    str(task.get("title") or ""),
                )
            for warning in result.get("warnings") or []:
                session.surface.emit_warning(str(warning))
            session.surface.emit_info(f"forge_plan_completed {record.plan_id}")
        except BridgeCancellationError as e:
            message = str(redact_secrets(e.reason or "cancelled_by_user"))
            try:
                _stop_heartbeat()
                self._forge_request_ledger.cancel(_current_lease(), error_code="cancelled_by_user")
            except ForgeRequestLedgerError:
                message = "forge_plan_worker_lease_lost"
            with self._state_lock:
                _mark_job_completed(
                    job,
                    status="cancelled" if message != "forge_plan_worker_lease_lost" else "failed",
                    exit_code=130 if message != "forge_plan_worker_lease_lost" else 1,
                    error=message,
                )
                _reconcile_session_job_state(session)
            session.surface.emit_warning(f"forge_plan_cancelled {job.job_id} reason={message}")
        except ProtocolError as e:
            message = str(redact_secrets(e.message))
            with suppress(ForgeRequestLedgerError):
                _stop_heartbeat()
                self._forge_request_ledger.fail(_current_lease(), error_code=e.code)
            with self._state_lock:
                _mark_job_completed(job, status="failed", exit_code=1, error=message)
                _reconcile_session_job_state(session)
            session.surface.emit_error(e.code, message, False)
        except ForgeRequestLeaseLost:
            heartbeat_stop.set()
            with self._state_lock:
                _mark_job_completed(
                    job,
                    status="failed",
                    exit_code=1,
                    error="forge_plan_worker_lease_lost",
                )
                _reconcile_session_job_state(session)
            session.surface.emit_error(
                "forge_plan_indeterminate",
                "Forge Plan worker ownership was lost; the request will not be executed again automatically.",
                False,
            )
        except Exception as e:  # noqa: BLE001
            message = str(redact_secrets(f"Forge planner adapter failed: {e}"))
            with suppress(ForgeRequestLedgerError):
                _stop_heartbeat()
                self._forge_request_ledger.fail(_current_lease(), error_code="forge_plan_failed")
            with self._state_lock:
                _mark_job_completed(job, status="failed", exit_code=1, error=message)
                _reconcile_session_job_state(session)
            session.surface.emit_error("forge_plan_failed", message, False)
        finally:
            heartbeat_stop.set()
            with self._state_lock:
                _reconcile_session_job_state(session)
                self._prune_terminal_jobs_locked()
            session.surface.with_job(None)

    def _forge_plan_result(self, request: ProtocolRequest) -> dict[str, Any]:
        job_id = _required_str(request.params, "job_id", request_id=request.id)
        try:
            durable = self._forge_request_ledger.get(job_id)
        except ForgeRequestLedgerError as exc:
            raise ProtocolError(
                "forge_plan_durability_error", str(exc), request_id=request.id
            ) from exc
        if durable is not None:
            if durable.state in {ForgeRequestState.QUEUED, ForgeRequestState.RUNNING}:
                return {
                    "session_id": durable.session_id,
                    "job_id": durable.job_id,
                    "plan_id": durable.plan_id,
                    "status": durable.state.value,
                    "state": durable.state.value,
                    "complete": False,
                    "cancellable": True,
                    "cancellation_requested": False,
                }
            if durable.state is ForgeRequestState.INDETERMINATE:
                raise ProtocolError(
                    "forge_plan_indeterminate",
                    "Forge Plan execution became indeterminate after its worker lease expired. It will not be executed again automatically; review workspace artifacts before starting a new request.",
                    request_id=request.id,
                )
            if durable.state is ForgeRequestState.CANCELLED:
                return {
                    "session_id": durable.session_id,
                    "job_id": durable.job_id,
                    "plan_id": durable.plan_id,
                    "status": "cancelled",
                    "state": "cancelled",
                    "complete": True,
                    "cancelled": True,
                    "cancellation_reason": durable.error_code,
                    "job": _durable_forge_job_summary(durable),
                }
            if durable.state is ForgeRequestState.FAILED:
                raise ProtocolError(
                    "forge_plan_failed",
                    "Forge Plan job failed.",
                    request_id=request.id,
                )
            if durable.plan_id is None:
                raise ProtocolError(
                    "forge_plan_failed",
                    "Forge Plan job completed without a persisted plan.",
                    request_id=request.id,
                )
            return self._reconstruct_durable_forge_plan_result(durable, request_id=request.id)

        job = self._require_job(job_id, request_id=request.id)
        if job.kind != "forge_plan":
            raise ProtocolError(
                "invalid_job",
                "Job is not a Forge Plan job.",
                request_id=request.id,
            )
        if job.status in ACTIVE_JOB_STATUSES:
            return {
                "session_id": job.session_id,
                "job_id": job.job_id,
                "plan_id": job.plan_id,
                "status": job.status,
                "state": job.status,
                "complete": False,
                "cancellable": bool(job.cancellable and job.status in {"queued", "running"}),
                "cancellation_requested": job.cancellation_event.is_set(),
            }
        if job.status == "cancelled":
            return {
                "session_id": job.session_id,
                "job_id": job.job_id,
                "plan_id": job.plan_id,
                "status": "cancelled",
                "state": "cancelled",
                "complete": True,
                "cancelled": True,
                "cancellation_reason": job.cancellation_reason,
                "job": _job_summary(job),
            }
        if job.status == "failed":
            raise ProtocolError(
                "forge_plan_failed",
                job.error or "Forge Plan job failed.",
                request_id=request.id,
            )
        if job.result is None:
            raise ProtocolError(
                "forge_plan_failed",
                "Forge Plan job completed without a result.",
                request_id=request.id,
            )
        return job.result

    def _forge_list(self, request: ProtocolRequest) -> dict[str, Any]:
        session_id = _optional_str(request.params, "session_id", request_id=request.id)
        if session_id:
            session = self._require_session(session_id, request_id=request.id)
            root = session.root
            active = self._forge_records_for_session(session.session_id)
        else:
            workspace = _required_str(request.params, "workspace", request_id=request.id)
            binding = _resolve_workspace(Path(workspace), request_id=request.id)
            root = binding.workspace_context.workspace_root
            active = []
        return list_persisted_forge_plans(
            workspace_root=root,
            session_id=session_id,
            active_records=active,
            max_items=request.params.get("max_items"),
        )

    def _forge_open(self, request: ProtocolRequest) -> dict[str, Any]:
        plan_id = _required_str(request.params, "plan_id", request_id=request.id)
        created_session = False
        session_id = _optional_str(request.params, "session_id", request_id=request.id)
        if session_id:
            session = self._require_session(session_id, request_id=request.id)
        else:
            workspace = _required_str(request.params, "workspace", request_id=request.id)
            binding = _resolve_workspace(Path(workspace), request_id=request.id)
            session = self._create_plan_inspection_session(binding.workspace_context.workspace_root)
            created_session = True
        try:
            record, plan = open_persisted_forge_plan(
                workspace_root=session.root,
                session_id=session.session_id,
                plan_id=plan_id,
            )
        except ProtocolError as e:
            if created_session:
                self._close_session(session)
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        with self._state_lock:
            self._forge_plans[record.plan_id] = record
            session.artifact_store.add_root(
                ArtifactRoot(
                    forge_artifact_root_name(record.plan_id),
                    validate_forge_run_artifact_root(record),
                )
            )
        result = forge_plan_result(record, plan, created_session=created_session)
        session.surface.emit_status_update(mode=session.mode)
        session.surface.emit_info(f"forge_plan_opened {record.plan_id}")
        return result

    def _forge_status(self, request: ProtocolRequest) -> dict[str, Any]:
        session_id = _required_str(request.params, "session_id", request_id=request.id)
        self._require_session(session_id, request_id=request.id)
        job_id = _optional_str(request.params, "job_id", request_id=request.id)
        if job_id and not request.params.get("plan_id"):
            job = self._require_job(job_id, request_id=request.id)
            if job.session_id != session_id or job.kind != "forge_plan":
                raise ProtocolError(
                    "job_not_found",
                    "Forge Plan job was not found for the session.",
                    request_id=request.id,
                )
            if job.status in ACTIVE_JOB_STATUSES:
                return {
                    "session_id": job.session_id,
                    "job_id": job.job_id,
                    "plan_id": job.plan_id,
                    "status": job.status,
                    "state": job.status,
                    "complete": False,
                    "cancellable": bool(job.cancellable and job.status in {"queued", "running"}),
                    "cancellation_requested": job.cancellation_event.is_set(),
                }
            if job.status == "failed":
                raise ProtocolError(
                    "forge_plan_failed",
                    job.error or "Forge Plan job failed.",
                    request_id=request.id,
                )
            if job.result is None:
                raise ProtocolError(
                    "forge_plan_failed",
                    "Forge Plan job completed without a result.",
                    request_id=request.id,
                )
            return job.result
        plan_id = _required_str(request.params, "plan_id", request_id=request.id)
        record = self._require_forge_plan(
            session_id=session_id,
            plan_id=plan_id,
            request_id=request.id,
        )
        try:
            plan = load_recorded_plan(record)
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        return forge_status_result(record, plan)

    def _forge_show(self, request: ProtocolRequest) -> dict[str, Any]:
        session, record, plan = self._forge_request_context(request, migrate_legacy=False)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        _ = session
        try:
            return forge_show_result(record, plan, cfg=cfg)
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e

    def _forge_plan_get_state(self, request: ProtocolRequest) -> dict[str, Any]:
        _session, record, plan = self._forge_request_context(request, migrate_legacy=False)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        try:
            return forge_plan_state_result(record, plan, cfg=cfg)
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e

    def _forge_plan_set_assistant(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        instruction = _required_str(request.params, "instruction", request_id=request.id)
        # The lock covers only the plan read-modify-write; the response payload
        # (diff/asset scans) renders lock-free from the request-local plan.
        with self._state_lock:
            session, record, plan = self._forge_request_context(request, migrate_legacy=False)
            try:
                outcome = forge_plan_set_assistant_apply(
                    record,
                    plan,
                    instruction=instruction,
                    expected_revision=request.params.get("expected_revision"),
                )
            except ProtocolError as e:
                raise ProtocolError(e.code, e.message, request_id=request.id) from e
        try:
            result = forge_plan_edit_render(record, plan, cfg=cfg, outcome=outcome)
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        session.surface.emit_status_update(mode=session.mode)
        session.surface.emit_info(f"forge_plan_assistant_updated {record.plan_id}")
        return result

    def _forge_plan_set_goal(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        goal = _required_str(request.params, "goal", request_id=request.id)
        with self._state_lock:
            session, record, plan = self._forge_request_context(request, migrate_legacy=False)
            try:
                outcome = forge_plan_set_goal_apply(
                    record,
                    plan,
                    goal=goal,
                    expected_revision=request.params.get("expected_revision"),
                )
            except ProtocolError as e:
                raise ProtocolError(e.code, e.message, request_id=request.id) from e
        try:
            result = forge_plan_edit_render(record, plan, cfg=cfg, outcome=outcome)
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        session.surface.emit_status_update(mode=session.mode)
        session.surface.emit_info(f"forge_plan_goal_updated {record.plan_id}")
        return result

    def _forge_plan_update_task(self, request: ProtocolRequest) -> dict[str, Any]:
        has_update = any(key in request.params for key in ("title", "body", "status"))
        if has_update:
            self._require_workspace_trusted(request)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        task_id = _required_str(request.params, "task_id", request_id=request.id)
        title = _optional_str(request.params, "title", request_id=request.id)
        body = _optional_str(request.params, "body", request_id=request.id)
        status = _optional_str(request.params, "status", request_id=request.id)
        with self._state_lock:
            session, record, plan = self._forge_request_context(request, migrate_legacy=False)
            try:
                outcome = forge_plan_update_task_apply(
                    record,
                    plan,
                    task_id=task_id,
                    title=title,
                    body=body,
                    status=status,
                    expected_revision=request.params.get("expected_revision"),
                )
            except ProtocolError as e:
                raise ProtocolError(e.code, e.message, request_id=request.id) from e
        try:
            result = forge_plan_edit_render(record, plan, cfg=cfg, outcome=outcome)
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        if has_update:
            session.surface.emit_status_update(mode=session.mode)
            session.surface.emit_info(f"forge_plan_task_updated {record.plan_id}")
        return result

    def _forge_plan_validate(self, request: ProtocolRequest) -> dict[str, Any]:
        _session, record, plan = self._forge_request_context(request, migrate_legacy=False)
        try:
            return forge_plan_validate_result(record, plan)
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e

    def _forge_plan_regenerate(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        instruction = _optional_str(request.params, "instruction", request_id=request.id)
        focus = _optional_str(request.params, "focus", request_id=request.id)
        # Snapshot under the lock; the planner runs lock-free on the snapshot
        # and the commit re-checks the revision, so concurrent bridge work
        # (events, jobs, cancellation) never waits on a provider round-trip.
        with self._state_lock:
            session, record, plan = self._forge_request_context(request, migrate_legacy=False)
        try:
            computation = forge_plan_regenerate_compute(
                record,
                plan,
                instruction=instruction,
                focus=focus,
                expected_revision=request.params.get("expected_revision"),
                cfg=cfg,
            )
            with self._state_lock:
                forge_plan_regenerate_commit(record, computation)
            result = forge_plan_regenerate_render(record, computation, cfg=cfg)
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        session.surface.emit_status_update(mode=session.mode)
        session.surface.emit_info(f"forge_plan_regenerated {record.plan_id}")
        return result

    def _forge_plan_regenerate_start(
        self, request: ProtocolRequest
    ) -> tuple[dict[str, Any], Callable[[], None]]:
        self._require_workspace_trusted(request)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        instruction = _optional_str(request.params, "instruction", request_id=request.id)
        focus = _optional_str(request.params, "focus", request_id=request.id)
        expected_revision = request.params.get("expected_revision")
        with self._state_lock:
            session, record, plan = self._forge_request_context(request, migrate_legacy=False)
            try:
                ensure_expected_plan_revision(plan, expected_revision)
            except ProtocolError as e:
                raise ProtocolError(e.code, e.message, request_id=request.id) from e
            _reconcile_session_job_state(session)
            if _job_is_active(session.active_job):
                raise ProtocolError(
                    "session_busy",
                    "Session already has a running job.",
                    request_id=request.id,
                )
            job = BridgeJob(
                job_id=_make_job_id(),
                session_id=session.session_id,
                created_at=_now_iso(),
                kind="forge_plan_regenerate",
                plan_id=record.plan_id,
            )
            self._register_job_locked(session, job)

        def _start() -> None:
            thread = threading.Thread(
                target=self._run_forge_plan_regenerate_job,
                args=(session, job, record, plan, instruction, focus, expected_revision, cfg),
                name=f"alysis-ide-forge-plan-regenerate-{job.job_id}",
                daemon=True,
            )
            self._start_job_thread(session, job, thread)

        return {
            "session_id": session.session_id,
            "plan_id": record.plan_id,
            "job_id": job.job_id,
            "status": "started",
        }, _start

    def _run_forge_plan_regenerate_job(
        self,
        session: BridgeSession,
        job: BridgeJob,
        record: ForgePlanRecord,
        plan: dict[str, Any],
        instruction: str | None,
        focus: str | None,
        expected_revision: Any,
        cfg: Any,
    ) -> None:
        session.surface.with_job(job.job_id)
        try:
            with self._state_lock:
                _mark_job_running(job)
            session.surface.emit_status_update(mode=session.mode, model=getattr(cfg, "model", None))
            session.surface.emit_info(f"forge_plan_regenerate_started {job.job_id}")
            _check_job_cancelled(job)
            computation = forge_plan_regenerate_compute(
                record,
                plan,
                instruction=instruction,
                focus=focus,
                expected_revision=expected_revision,
                cfg=cfg,
                cancellation_token=BridgeCancellationToken(job.cancellation_event),
            )
            _check_job_cancelled(job)
            with self._state_lock:
                forge_plan_regenerate_commit(record, computation)
            result = forge_plan_regenerate_render(record, computation, cfg=cfg)
            with self._state_lock:
                _mark_job_completed(job, status="completed", exit_code=0, result=result)
                _reconcile_session_job_state(session)
            session.surface.emit_status_update(mode=session.mode, model=getattr(cfg, "model", None))
            session.surface.emit_info(f"forge_plan_regenerated {record.plan_id}")
        except BridgeCancellationError as e:
            message = str(redact_secrets(e.reason or "cancelled_by_user"))
            with self._state_lock:
                _mark_job_completed(job, status="cancelled", exit_code=130, error=message)
                _reconcile_session_job_state(session)
            session.surface.emit_warning(
                f"forge_plan_regenerate_cancelled {job.job_id} reason={message}"
            )
        except ProtocolError as e:
            message = str(redact_secrets(e.message))
            with self._state_lock:
                job.error_code = e.code
                _mark_job_completed(job, status="failed", exit_code=1, error=message)
                _reconcile_session_job_state(session)
            session.surface.emit_error(e.code, message, False)
        except Exception as e:  # noqa: BLE001
            message = str(redact_secrets(f"Forge plan regeneration failed: {e}"))
            with self._state_lock:
                _mark_job_completed(job, status="failed", exit_code=1, error=message)
                _reconcile_session_job_state(session)
            session.surface.emit_error("forge_plan_regenerate_failed", message, False)
        finally:
            with self._state_lock:
                _reconcile_session_job_state(session)
                self._prune_terminal_jobs_locked()
            session.surface.with_job(None)

    def _forge_plan_regenerate_job_result(self, request: ProtocolRequest) -> dict[str, Any]:
        job_id = _required_str(request.params, "job_id", request_id=request.id)
        job = self._require_job(job_id, request_id=request.id)
        if job.kind != "forge_plan_regenerate":
            raise ProtocolError(
                "invalid_job",
                "Job is not a Forge Plan regeneration job.",
                request_id=request.id,
            )
        if job.status in ACTIVE_JOB_STATUSES:
            return {
                "session_id": job.session_id,
                "job_id": job.job_id,
                "plan_id": job.plan_id,
                "status": job.status,
                "state": job.status,
                "complete": False,
                "cancellable": bool(job.cancellable and job.status in {"queued", "running"}),
                "cancellation_requested": job.cancellation_event.is_set(),
            }
        if job.status == "cancelled":
            return {
                "session_id": job.session_id,
                "job_id": job.job_id,
                "plan_id": job.plan_id,
                "status": "cancelled",
                "state": "cancelled",
                "complete": True,
                "cancelled": True,
                "cancellation_reason": job.cancellation_reason,
                "job": _job_summary(job),
            }
        if job.status == "failed":
            raise ProtocolError(
                job.error_code or "forge_plan_regenerate_failed",
                job.error or "Forge Plan regeneration job failed.",
                request_id=request.id,
            )
        if job.result is None:
            raise ProtocolError(
                "forge_plan_regenerate_failed",
                "Forge Plan regeneration job completed without a result.",
                request_id=request.id,
            )
        return job.result

    def _code_review_start(
        self, request: ProtocolRequest
    ) -> tuple[dict[str, Any], Callable[[], None]]:
        self._require_workspace_trusted(request)
        session_id = _required_str(request.params, "session_id", request_id=request.id)
        review_request = _code_review_request(request.params, request_id=request.id)
        with self._state_lock:
            session = self._require_session(session_id, request_id=request.id)
            _reconcile_session_job_state(session)
            if _job_is_active(session.active_job):
                raise ProtocolError(
                    "session_busy",
                    "Session already has a running job.",
                    request_id=request.id,
                )
            job = BridgeJob(
                job_id=_make_job_id(),
                session_id=session.session_id,
                created_at=_now_iso(),
                kind="code_review",
            )
            self._register_job_locked(session, job)

        def _start() -> None:
            thread = threading.Thread(
                target=self._run_code_review_job,
                args=(session, job, review_request),
                name=f"alysis-ide-code-review-{job.job_id}",
                daemon=True,
            )
            self._start_job_thread(session, job, thread)

        return {
            "session_id": session.session_id,
            "job_id": job.job_id,
            "scope": review_request.scope.value,
            "status": "started",
        }, _start

    def _run_code_review_job(
        self,
        session: BridgeSession,
        job: BridgeJob,
        review_request: ReviewRequest,
    ) -> None:
        session.surface.with_job(job.job_id)
        try:
            with self._state_lock:
                _mark_job_running(job)
            session.surface.emit_info(
                f"code_review_started {job.job_id} scope={review_request.scope.value}"
            )
            _check_job_cancelled(job)
            if self._code_review_runner is not None:
                review_payload = self._code_review_runner(
                    session.root,
                    session.agent_session,
                    review_request,
                )
            else:
                client = getattr(session.agent_session, "client", None)
                if client is None:
                    raise RuntimeError("Active session has no provider client for code review.")
                review_payload = (
                    CodeReviewEngine(
                        session.root,
                        ChatReviewerClient(client),
                    )
                    .review(review_request)
                    .to_dict()
                )
            _check_job_cancelled(job)
            result = {
                "session_id": session.session_id,
                "job_id": job.job_id,
                "status": "completed",
                **_json_object(review_payload),
            }
            with self._state_lock:
                _mark_job_completed(job, status="completed", exit_code=0, result=result)
                _reconcile_session_job_state(session)
            session.surface.emit_status_update(mode=session.mode)
            session.surface.emit_info(
                f"code_review_completed {job.job_id} scope={review_request.scope.value}"
            )
        except BridgeCancellationError as e:
            message = str(redact_secrets(e.reason or "cancelled_by_user"))
            with self._state_lock:
                _mark_job_completed(job, status="cancelled", exit_code=130, error=message)
                _reconcile_session_job_state(session)
            session.surface.emit_warning(f"code_review_cancelled {job.job_id} reason={message}")
        except Exception as e:  # noqa: BLE001
            message = str(redact_secrets(f"Code review failed: {e}"))
            with self._state_lock:
                job.error_code = "code_review_failed"
                _mark_job_completed(job, status="failed", exit_code=1, error=message)
                _reconcile_session_job_state(session)
            session.surface.emit_error("code_review_failed", message, False)
        finally:
            with self._state_lock:
                _reconcile_session_job_state(session)
                self._prune_terminal_jobs_locked()
            session.surface.with_job(None)

    def _code_review_job_result(self, request: ProtocolRequest) -> dict[str, Any]:
        job_id = _required_str(request.params, "job_id", request_id=request.id)
        job = self._require_job(job_id, request_id=request.id)
        if job.kind != "code_review":
            raise ProtocolError(
                "invalid_job",
                "Job is not a generic code review job.",
                request_id=request.id,
            )
        if job.status in ACTIVE_JOB_STATUSES:
            return {
                "session_id": job.session_id,
                "job_id": job.job_id,
                "status": job.status,
                "state": job.status,
                "complete": False,
                "cancellable": bool(job.cancellable and job.status in {"queued", "running"}),
                "cancellation_requested": job.cancellation_event.is_set(),
            }
        if job.status == "cancelled":
            return {
                "session_id": job.session_id,
                "job_id": job.job_id,
                "status": "cancelled",
                "state": "cancelled",
                "complete": True,
                "cancelled": True,
                "cancellation_reason": job.cancellation_reason,
                "job": _job_summary(job),
            }
        if job.status == "failed":
            raise ProtocolError(
                job.error_code or "code_review_failed",
                job.error or "Code review job failed.",
                request_id=request.id,
            )
        if job.result is None:
            raise ProtocolError(
                "code_review_failed",
                "Code review job completed without a result.",
                request_id=request.id,
            )
        return job.result

    def _forge_review(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        session, record, plan = self._forge_request_context(request, migrate_legacy=False)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        task_id = _required_str(request.params, "task_id", request_id=request.id)
        try:
            result = forge_review_result(record, plan, task_id=task_id, cfg=cfg)
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        session.surface.emit_status_update(mode=session.mode, model=getattr(cfg, "model", None))
        session.surface.emit_info(f"forge_review_completed {record.plan_id} task_id={task_id}")
        return result

    def _forge_attach(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        session, record, _plan = self._forge_request_context(request, migrate_legacy=True)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        source = _source_path_param(request.params, request_id=request.id)
        try:
            result = forge_attach_result(
                record,
                source_path=Path(source),
                cfg=cfg,
                title=_optional_str(request.params, "title", request_id=request.id),
                description=_optional_str(request.params, "description", request_id=request.id)
                or "",
            )
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        session.surface.emit_status_update(mode=session.mode)
        session.surface.emit_info(f"forge_asset_attached {record.plan_id}")
        return result

    def _forge_assets_list(self, request: ProtocolRequest) -> dict[str, Any]:
        _session, record, _plan = self._forge_request_context(request, migrate_legacy=False)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        try:
            return forge_assets_list_result(
                record,
                cfg=cfg,
                include_deleted=_optional_bool(
                    request.params,
                    "include_deleted",
                    default=False,
                    request_id=request.id,
                ),
            )
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e

    def _forge_assets_show(self, request: ProtocolRequest) -> dict[str, Any]:
        _session, record, _plan = self._forge_request_context(request, migrate_legacy=False)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        try:
            return forge_assets_show_result(
                record,
                asset_id=_required_str(request.params, "asset_id", request_id=request.id),
                cfg=cfg,
            )
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e

    def _forge_assets_add(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        session, record, _plan = self._forge_request_context(request, migrate_legacy=True)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        source = _source_path_param(request.params, request_id=request.id)
        try:
            result = forge_assets_add_result(
                record,
                source_path=Path(source),
                title=_optional_str(request.params, "title", request_id=request.id),
                description=_optional_str(request.params, "description", request_id=request.id)
                or "",
                pinned=_optional_bool(
                    request.params, "pinned", default=False, request_id=request.id
                ),
                wait=_optional_bool(request.params, "wait", default=False, request_id=request.id),
                link=_optional_bool(request.params, "link", default=False, request_id=request.id),
                cfg=cfg,
            )
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        session.surface.emit_status_update(mode=session.mode)
        session.surface.emit_info(f"forge_asset_added {record.plan_id}")
        return result

    def _forge_assets_delete(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        self._require_confirmation(request)
        session, record, _plan = self._forge_request_context(request, migrate_legacy=True)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        try:
            result = forge_assets_delete_result(
                record,
                asset_id=_required_str(request.params, "asset_id", request_id=request.id),
                cfg=cfg,
            )
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        session.surface.emit_status_update(mode=session.mode)
        session.surface.emit_info(f"forge_asset_deleted {record.plan_id}")
        return result

    def _forge_assets_edit(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        session, record, _plan = self._forge_request_context(request, migrate_legacy=True)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        try:
            result = forge_assets_edit_result(
                record,
                asset_id=_required_str(request.params, "asset_id", request_id=request.id),
                cfg=cfg,
                title=_optional_str(request.params, "title", request_id=request.id),
                description=_optional_str(request.params, "description", request_id=request.id),
                pinned=_optional_bool_or_none(request.params, "pinned", request_id=request.id),
                refresh=_optional_bool(
                    request.params, "refresh", default=False, request_id=request.id
                ),
            )
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        session.surface.emit_status_update(mode=session.mode)
        session.surface.emit_info(f"forge_asset_updated {record.plan_id}")
        return result

    def _forge_assets_refresh(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        session, record, _plan = self._forge_request_context(request, migrate_legacy=True)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        try:
            result = forge_assets_refresh_result(
                record,
                asset_id=_required_str(request.params, "asset_id", request_id=request.id),
                cfg=cfg,
            )
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        session.surface.emit_status_update(mode=session.mode)
        session.surface.emit_info(f"forge_asset_refreshed {record.plan_id}")
        return result

    def _forge_assets_cancel_pending(self, request: ProtocolRequest) -> dict[str, Any]:
        session, record, _plan = self._forge_request_context(request, migrate_legacy=False)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        try:
            result = forge_assets_cancel_pending_result(record, cfg=cfg)
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        session.surface.emit_status_update(mode=session.mode)
        return result

    def _forge_assets_check_plan(self, request: ProtocolRequest) -> dict[str, Any]:
        _session, record, plan = self._forge_request_context(request, migrate_legacy=False)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        try:
            return forge_assets_check_plan_result(record, plan, cfg=cfg)
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e

    def _forge_assets_prune_legacy(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        session, record, plan = self._forge_request_context(request, migrate_legacy=False)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        try:
            result = forge_assets_prune_legacy_result(
                record,
                plan,
                cfg=cfg,
                yes=_optional_bool(request.params, "yes", default=False, request_id=request.id),
            )
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        session.surface.emit_status_update(mode=session.mode)
        if result.get("deleted"):
            session.surface.emit_info(f"forge_legacy_assets_pruned {record.plan_id}")
        return result

    def _forge_execute(
        self, request: ProtocolRequest
    ) -> tuple[dict[str, Any], Callable[[], None] | None]:
        params = request.params
        session_id = _required_str(params, "session_id", request_id=request.id)
        session = self._require_session(session_id, request_id=request.id)
        plan_id = _required_str(params, "plan_id", request_id=request.id)
        record = self._require_forge_plan(
            session_id=session_id,
            plan_id=plan_id,
            request_id=request.id,
        )
        task_ids = _optional_task_ids(params.get("task_ids"), request_id=request.id)
        dry_run = _optional_bool(request.params, "dry_run", default=False, request_id=request.id)
        if dry_run:
            return self._forge_execute_preview(request), None
        mode = _forge_execute_mode_param(params, request_id=request.id)
        if mode != "review":
            raise ProtocolError(
                "forge_execute_unsupported",
                "Forge Execute v1 supports review mode only.",
                request_id=request.id,
            )
        if not task_ids:
            raise ProtocolError(
                "missing_field",
                "forge.execute requires explicit task_ids.",
                request_id=request.id,
            )
        workspace_trusted = _optional_bool_or_none(
            params,
            "workspace_trusted",
            request_id=request.id,
        )
        sandbox_profile = (
            _optional_str(params, "sandbox_profile", request_id=request.id) or "default"
        )
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, params, request_id=request.id)
        max_steps, no_log = _forge_execute_policy_params(cfg, params, request_id=request.id)
        try:
            plan = load_recorded_plan(record, migrate_legacy=False)
            preview = forge_execute_preview_result(
                record,
                plan,
                task_ids=task_ids,
                execution_mode=mode,
                workspace_trusted=workspace_trusted,
                sandbox_profile=sandbox_profile,
                max_steps=max_steps,
                no_log=no_log,
                cfg=cfg,
            )
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        if not preview["preview_ready"]:
            blockers = "; ".join(str(item) for item in preview.get("missing_prerequisites") or [])
            raise ProtocolError(
                "forge_execute_prerequisites_failed",
                str(redact_secrets(blockers or "Forge Execute prerequisites are not satisfied.")),
                request_id=request.id,
            )
        with self._state_lock:
            _reconcile_session_job_state(session)
            if _job_is_active(session.active_job):
                raise ProtocolError(
                    "session_busy",
                    "Session already has a running job.",
                    request_id=request.id,
                )
            job = BridgeJob(
                job_id=_make_job_id(),
                session_id=session.session_id,
                created_at=_now_iso(),
                kind="forge_execute",
                plan_id=record.plan_id,
            )
            self._register_job_locked(session, job)

        def _start() -> None:
            thread = threading.Thread(
                target=self._run_forge_execute_job,
                args=(
                    session,
                    job,
                    record,
                    task_ids,
                    workspace_trusted,
                    sandbox_profile,
                    cfg,
                    max_steps,
                    no_log,
                ),
                name=f"alysis-ide-forge-execute-{job.job_id}",
                daemon=True,
            )
            self._start_job_thread(session, job, thread)

        return {
            "session_id": session.session_id,
            "plan_id": record.plan_id,
            "job_id": job.job_id,
            "status": "started",
        }, _start

    def _run_forge_execute_job(
        self,
        session: BridgeSession,
        job: BridgeJob,
        record: ForgePlanRecord,
        task_ids: list[str],
        workspace_trusted: bool | None,
        sandbox_profile: str,
        cfg: Any,
        max_steps: int,
        no_log: bool,
    ) -> None:
        session.surface.with_job(job.job_id)
        try:
            with self._state_lock:
                _mark_job_running(job)
            _check_job_cancelled(job)
            plan = load_recorded_plan(record, migrate_legacy=False)
            result = forge_execute_review_job_result(
                record,
                plan,
                task_ids=task_ids,
                workspace_trusted=workspace_trusted,
                sandbox_profile=sandbox_profile,
                cfg=cfg,
                surface=session.surface,
                job_id=job.job_id,
                max_steps=max_steps,
                no_log=no_log,
                agent_runner=self._forge_execute_agent_runner,
                cancellation_token=BridgeCancellationToken(job.cancellation_event),
            )
            _check_job_cancelled(job)
            # Data-outcome job status (aligned with forge.swarm): an execute
            # run that finished is a COMPLETED job whose result payload
            # carries the task-level outcome; exit_code keeps the shell
            # semantic. Only engine exceptions mark the job failed.
            exit_code = 0 if result.get("status") == "completed" else 1
            with self._state_lock:
                _mark_job_completed(job, status="completed", exit_code=exit_code, result=result)
                _reconcile_session_job_state(session)
            self._emit_forge_execute_terminal_event(session, job, record, cfg)
        except BridgeCancellationError as e:
            message = str(redact_secrets(e.reason or "cancelled_by_user"))
            with self._state_lock:
                _mark_job_completed(job, status="cancelled", exit_code=130, error=message)
                _reconcile_session_job_state(session)
            self._emit_forge_execute_terminal_event(session, job, record, cfg)
            session.surface.emit_warning(f"forge_execute_cancelled {job.job_id} reason={message}")
        except ProtocolError as e:
            message = str(redact_secrets(e.message))
            with self._state_lock:
                _mark_job_completed(job, status="failed", exit_code=1, error=message)
                _reconcile_session_job_state(session)
            self._emit_forge_execute_terminal_event(session, job, record, cfg)
            session.surface.emit_error(e.code, message, False)
        except Exception as e:  # noqa: BLE001
            message = str(redact_secrets(f"Forge Execute failed: {e}"))
            with self._state_lock:
                _mark_job_completed(job, status="failed", exit_code=1, error=message)
                _reconcile_session_job_state(session)
            self._emit_forge_execute_terminal_event(session, job, record, cfg)
            session.surface.emit_error("forge_execute_failed", message, False)
        finally:
            with self._state_lock:
                _reconcile_session_job_state(session)
                self._prune_terminal_jobs_locked()
            session.surface.with_job(None)

    def _emit_forge_execute_terminal_event(
        self,
        session: BridgeSession,
        job: BridgeJob,
        record: ForgePlanRecord,
        cfg: Any,
    ) -> None:
        session.surface.emit_status_update(mode="review", model=getattr(cfg, "model", None))
        session.surface.emit_info(
            f"forge_execute_{job.status} {record.plan_id} "
            f"job_id={job.job_id} exit_code={job.exit_code}"
        )

    def _forge_execute_preview(self, request: ProtocolRequest) -> dict[str, Any]:
        params = request.params
        session_id = _required_str(params, "session_id", request_id=request.id)
        session = self._require_session(session_id, request_id=request.id)
        plan_id = _required_str(params, "plan_id", request_id=request.id)
        record = self._require_forge_plan(
            session_id=session_id,
            plan_id=plan_id,
            request_id=request.id,
        )
        try:
            plan = load_recorded_plan(record, migrate_legacy=False)
            cfg = clone_cfg(load_config())
            _apply_config_overrides(cfg, params, request_id=request.id)
            max_steps, no_log = _forge_execute_policy_params(cfg, params, request_id=request.id)
            preview = forge_execute_preview_result(
                record,
                plan,
                task_ids=_optional_task_ids(params.get("task_ids"), request_id=request.id),
                execution_mode=_forge_execute_preview_mode_param(params, request_id=request.id),
                workspace_trusted=_optional_bool_or_none(
                    params,
                    "workspace_trusted",
                    request_id=request.id,
                ),
                sandbox_profile=_optional_str(
                    params,
                    "sandbox_profile",
                    request_id=request.id,
                )
                or "default",
                max_steps=max_steps,
                no_log=no_log,
                cfg=cfg,
            )
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        session.surface.emit_status_update(mode=session.mode)
        if preview["preview_ready"]:
            session.surface.emit_info(f"forge_execute_preview_ready {record.plan_id}")
        else:
            for prerequisite in preview.get("missing_prerequisites") or []:
                session.surface.emit_warning(str(prerequisite))
        if not preview["real_execution_supported"]:
            session.surface.emit_warning(str(preview["unsupported_reason"]))
        return preview

    def _forge_cancel(self, request: ProtocolRequest) -> dict[str, Any]:
        session_id = _required_str(request.params, "session_id", request_id=request.id)
        session = self._require_session(session_id, request_id=request.id)
        plan_id = _required_str(request.params, "plan_id", request_id=request.id)
        self._require_forge_plan(session_id=session_id, plan_id=plan_id, request_id=request.id)
        reason = (
            _optional_str(request.params, "reason", request_id=request.id) or "cancelled_by_user"
        )
        cancellation_status: str | None = None
        with self._state_lock:
            _reconcile_session_job_state(session)
            job = session.active_job
            if not _job_is_active(job) or job is None or job.plan_id != plan_id:
                return {
                    "session_id": session.session_id,
                    "plan_id": plan_id,
                    "status": "no_active_job",
                    "state": "idle",
                    "job": None,
                }
            if job.kind not in {
                "forge_plan",
                "forge_plan_regenerate",
                "forge_review",
                "forge_execute",
                "forge_swarm",
            }:
                return {
                    "session_id": session.session_id,
                    "plan_id": plan_id,
                    "status": "non_cancellable",
                    "state": job.status,
                    "job": _job_summary(job),
                }
            result = _request_job_cancellation(job, reason)
            cancellation_status = str(result["status"])
            response = {
                "session_id": session.session_id,
                "plan_id": plan_id,
                "status": result["status"],
                "state": result["state"],
                "job_id": job.job_id,
                "job": _job_summary(job),
            }
        if cancellation_status == "cancellation_requested":
            self._cancel_pending_approvals(session, reason)
            session.surface.emit_warning(
                f"forge_cancellation_requested {job.job_id} reason={job.cancellation_reason}"
            )
        elif cancellation_status == "non_cancellable":
            session.surface.emit_warning(f"forge_cancel_rejected {job.job_id} non_cancellable")
        return response

    def _forge_swarm_start(
        self, request: ProtocolRequest
    ) -> tuple[dict[str, Any], Callable[[], None]]:
        self._require_workspace_trusted(request)
        params = request.params
        parallel = _positive_int_param(
            params, "parallel", default=2, upper=8, request_id=request.id
        )
        grants = _validated_approval_scope_grants(
            params.get("approval_scope_grants", []), request_id=request.id
        )
        idempotency_key = (
            _optional_str(params, "idempotency_key", request_id=request.id) or uuid.uuid4().hex
        )
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, params, request_id=request.id)
        with self._state_lock:
            session, record, plan = self._forge_request_context(request, migrate_legacy=False)
            permission_scope = _swarm_permission_scope(grants)
            action_grants = grants
            _reconcile_session_job_state(session)
            if _job_is_active(session.active_job):
                raise ProtocolError(
                    "session_busy",
                    "Session already has a running job.",
                    request_id=request.id,
                )
            coordinator = self._resumable_swarm_for_session(session, request_id=request.id)
            try:
                creation = coordinator.start_job(
                    session_id=session.session_id,
                    idempotency_key=idempotency_key,
                    execution_spec={
                        "plan_id": record.plan_id,
                        "parallel": parallel,
                        "usage_attribution": "isolated_job_sessions_v1",
                    },
                    permission_scope=permission_scope,
                )
            except ResumableSwarmError as exc:
                raise ProtocolError(
                    "resumable_swarm_error", str(exc), request_id=request.id
                ) from exc
            durable_status = creation.status
            if not creation.created and durable_status.state is not SwarmJobState.QUEUED:
                return {
                    "session_id": session.session_id,
                    "plan_id": record.plan_id,
                    **durable_status.public_payload(),
                    "created": False,
                }, lambda: None
            job = BridgeJob(
                job_id=durable_status.job_id,
                session_id=session.session_id,
                created_at=_now_iso(),
                kind="forge_swarm",
                plan_id=record.plan_id,
            )
            self._register_job_locked(session, job)
        if action_grants:
            # Launch-time pre-grants reuse the existing per-session scope
            # machinery: a matching dangerous action auto-allows and never
            # prompts (same semantics as "allow for session").
            with session.approval_lock:
                for grant in action_grants:
                    if not any(
                        existing.key == grant.key for existing in session.approved_approval_scopes
                    ):
                        session.approved_approval_scopes.append(grant)
            session.surface.emit_info(
                f"swarm_approval_scope_grants_recorded count={len(action_grants)}"
            )

        def _start() -> None:
            thread = threading.Thread(
                target=self._run_forge_swarm_job,
                args=(
                    session,
                    job,
                    record,
                    plan,
                    cfg,
                    parallel,
                    coordinator,
                    permission_scope,
                ),
                name=f"alysis-ide-forge-swarm-{job.job_id}",
                daemon=True,
            )
            self._start_job_thread(session, job, thread)

        return {
            "session_id": session.session_id,
            "plan_id": record.plan_id,
            "job_id": job.job_id,
            "status": "started",
            "parallel": parallel,
            "created": creation.created,
            "revision": durable_status.revision,
        }, _start

    def _forge_swarm_resume(
        self, request: ProtocolRequest
    ) -> tuple[dict[str, Any], Callable[[], None]]:
        self._require_workspace_trusted(request)
        grants = _validated_approval_scope_grants(
            request.params.get("approval_scope_grants", []), request_id=request.id
        )
        requested_parallel = _positive_int_param(
            request.params, "parallel", default=2, upper=8, request_id=request.id
        )
        expected_revision = _optional_int(
            request.params.get("expected_revision"), request_id=request.id
        )
        job_id = _required_str(request.params, "job_id", request_id=request.id)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        with self._state_lock:
            session, record, plan = self._forge_request_context(request, migrate_legacy=False)
            permission_scope, action_grants, fresh_permission_grant = (
                _swarm_resume_permission_scope(
                    grants,
                    session_id=session.session_id,
                    plan_id=record.plan_id,
                    job_id=job_id,
                    expected_revision=expected_revision,
                    request_id=request.id,
                )
            )
            _reconcile_session_job_state(session)
            if _job_is_active(session.active_job):
                raise ProtocolError(
                    "session_busy",
                    "Session already has a running job.",
                    request_id=request.id,
                )
            coordinator = self._resumable_swarm_for_session(session, request_id=request.id)
            try:
                coordinator.recover_stale_jobs(session_id=session.session_id)
                durable_status = coordinator.resume_job(
                    session_id=session.session_id,
                    job_id=job_id,
                    permission_scope=permission_scope,
                    fresh_permission_grant=fresh_permission_grant,
                    expected_revision=expected_revision,
                )
            except SwarmPermissionScopeChanged as exc:
                raise ProtocolError(
                    "swarm_permission_scope_changed", str(exc), request_id=request.id
                ) from exc
            except SwarmFreshPermissionGrantRequired as exc:
                raise ProtocolError(
                    "swarm_fresh_permission_grant_required", str(exc), request_id=request.id
                ) from exc
            except ResumableSwarmError as exc:
                raise ProtocolError(
                    "resumable_swarm_error", str(exc), request_id=request.id
                ) from exc
            job = BridgeJob(
                job_id=durable_status.job_id,
                session_id=session.session_id,
                created_at=_now_iso(),
                kind="forge_swarm",
                plan_id=record.plan_id,
            )
            self._register_job_locked(session, job)

        if action_grants:
            with session.approval_lock:
                for grant in action_grants:
                    if not any(
                        existing.key == grant.key for existing in session.approved_approval_scopes
                    ):
                        session.approved_approval_scopes.append(grant)

        def _start() -> None:
            thread = threading.Thread(
                target=self._run_forge_swarm_job,
                args=(
                    session,
                    job,
                    record,
                    plan,
                    cfg,
                    requested_parallel,
                    coordinator,
                    permission_scope,
                ),
                name=f"alysis-ide-forge-swarm-resume-{job.job_id}",
                daemon=True,
            )
            self._start_job_thread(session, job, thread)

        return {
            "session_id": session.session_id,
            "plan_id": record.plan_id,
            "status": "resumed",
            **durable_status.public_payload(),
        }, _start

    def _forge_swarm_list(self, request: ProtocolRequest) -> dict[str, Any]:
        session_id = _required_str(request.params, "session_id", request_id=request.id)
        session = self._require_session(session_id, request_id=request.id)
        limit = _positive_int_param(
            request.params, "limit", default=100, upper=200, request_id=request.id
        )
        coordinator = self._resumable_swarm_for_session(session, request_id=request.id)
        try:
            coordinator.recover_stale_jobs(session_id=session.session_id)
            jobs = coordinator.list_jobs(session_id=session.session_id, limit=limit)
        except ResumableSwarmError as exc:
            raise ProtocolError("resumable_swarm_error", str(exc), request_id=request.id) from exc
        payloads = [status.public_payload() for status in jobs]
        return {"session_id": session.session_id, "jobs": payloads, "count": len(payloads)}

    def _run_forge_swarm_job(
        self,
        session: BridgeSession,
        job: BridgeJob,
        record: ForgePlanRecord,
        plan: dict[str, Any],
        cfg: Any,
        parallel: int,
        coordinator: DurableResumableSwarmCoordinator,
        permission_scope: dict[str, Any],
    ) -> None:
        session.surface.with_job(job.job_id)
        lease: SwarmWorkerLease | None = None
        lease_lock = threading.RLock()
        heartbeat_stop = threading.Event()
        heartbeat_thread: threading.Thread | None = None
        heartbeat_error: list[ResumableSwarmError] = []
        usage_recorded_generations: set[int] = set()
        usage_sessions_dir = record.paths.execution_sessions_dir
        swarm_paths = record.paths

        def _current_lease() -> SwarmWorkerLease | None:
            with lease_lock:
                return lease

        def _stop_heartbeat() -> None:
            heartbeat_stop.set()
            if (
                heartbeat_thread is not None
                and heartbeat_thread is not threading.current_thread()
                and heartbeat_thread.is_alive()
            ):
                heartbeat_thread.join(timeout=1.0)

        def _record_usage(active_lease: SwarmWorkerLease) -> None:
            # Terminal handling can enter a secondary fail/interrupt path after
            # usage committed but result persistence failed. Recomputing the
            # delta at that point yields zero and reuses the same attempt key,
            # which the durable coordinator correctly treats as an idempotency
            # conflict. Keep the runner-side operation one-shot per fenced
            # generation so the original terminal error can still be stored.
            with lease_lock:
                if active_lease.generation in usage_recorded_generations:
                    return
            total = _forge_swarm_usage(usage_sessions_dir)
            current = coordinator.get_status(session_id=session.session_id, job_id=job.job_id).usage
            coordinator.record_usage(
                active_lease,
                _swarm_usage_delta(total, current),
                idempotency_key=f"attempt-{active_lease.generation}",
            )
            with lease_lock:
                usage_recorded_generations.add(active_lease.generation)

        def _interrupt_durable(*, error_code: str, error_summary: str) -> None:
            active_lease = _current_lease()
            if active_lease is None:
                return
            try:
                status = coordinator.get_status(session_id=session.session_id, job_id=job.job_id)
                if status.state is SwarmJobState.CANCELLED:
                    return
                _record_usage(active_lease)
                coordinator.interrupt(
                    active_lease,
                    error_code=error_code,
                    error_summary=error_summary,
                )
            except SwarmLeaseLost:
                with suppress(ResumableSwarmError):
                    coordinator.recover_stale_jobs(session_id=session.session_id)

        def _fail_durable(*, error_code: str, error_summary: str) -> None:
            active_lease = _current_lease()
            if active_lease is None:
                return
            try:
                status = coordinator.get_status(session_id=session.session_id, job_id=job.job_id)
                if status.state in {
                    SwarmJobState.CANCELLED,
                    SwarmJobState.INTERRUPTED,
                    SwarmJobState.SUCCEEDED,
                    SwarmJobState.FAILED,
                }:
                    return
                _record_usage(active_lease)
                coordinator.fail(
                    active_lease,
                    error_code=error_code,
                    error_summary=error_summary,
                )
            except SwarmLeaseLost:
                with suppress(ResumableSwarmError):
                    coordinator.recover_stale_jobs(session_id=session.session_id)

        try:
            lease = coordinator.claim_job(
                session_id=session.session_id,
                job_id=job.job_id,
                worker_id=f"ide-bridge:{self._prompt_queue_owner_id}",
                permission_scope=permission_scope,
            )
            with lease_lock:
                job.durable_swarm_lease = lease
            usage_attribution = lease.execution_spec.get("usage_attribution")
            if usage_attribution is not None:
                if usage_attribution != "isolated_job_sessions_v1":
                    raise ResumableSwarmError(
                        "Durable swarm execution has an invalid usage-attribution mode."
                    )
                usage_sessions_dir = _forge_swarm_job_sessions_dir(
                    record.paths.execution_sessions_dir,
                    job.job_id,
                )
                swarm_paths = replace(
                    record.paths,
                    execution_sessions_dir=usage_sessions_dir,
                )
            durable_plan_id = str(lease.execution_spec.get("plan_id") or "")
            if durable_plan_id != record.plan_id:
                raise ResumableSwarmError(
                    "Durable swarm execution no longer matches the selected Forge plan."
                )
            try:
                parallel = max(1, min(8, int(lease.execution_spec.get("parallel") or parallel)))
            except (TypeError, ValueError):
                raise ResumableSwarmError(
                    "Durable swarm execution has an invalid parallelism setting."
                ) from None

            def _heartbeat() -> None:
                nonlocal lease
                while not heartbeat_stop.wait(30.0):
                    active_lease = _current_lease()
                    if active_lease is None:
                        return
                    try:
                        if coordinator.should_cancel(active_lease):
                            job.cancellation_event.set()
                            return
                        renewed = coordinator.renew(active_lease)
                        with lease_lock:
                            lease = renewed
                            job.durable_swarm_lease = renewed
                    except ResumableSwarmError as exc:
                        heartbeat_error.append(exc)
                        job.cancellation_event.set()
                        return

            heartbeat_thread = threading.Thread(
                target=_heartbeat,
                name=f"alysis-ide-swarm-lease-{job.job_id}",
                daemon=True,
            )
            heartbeat_thread.start()
            with self._state_lock:
                _mark_job_running(job)
            session.surface.emit_status_update(mode=session.mode, model=getattr(cfg, "model", None))
            session.surface.emit_info(
                f"swarm_started {record.plan_id} job_id={job.job_id} parallel={parallel}"
            )
            _check_job_cancelled(job)
            runner = self._forge_swarm_runner or run_swarm

            def _worker_approval_router(
                *, task_id: str, request: ApprovalRequest
            ) -> ApprovalDecision:
                # Workers run WITHOUT --yes-style auto-approval: dangerous
                # actions route to the per-session approval system with task
                # attribution; the requesting worker thread pauses here while
                # siblings keep running.
                metadata = dict(request.metadata or {})
                metadata.setdefault("forge_swarm_task_id", task_id)
                metadata.setdefault("worker", f"forge_swarm:{task_id}")
                metadata.setdefault("job_id", job.job_id)
                annotated = replace(request, metadata=metadata)
                return session.surface.request_approval(annotated)

            # The engine runs lock-free in this job thread; forge.swarm.cancel
            # (and forge.cancel) set job.cancellation_event, which is the same
            # event the swarm engine honors at its cooperative checkpoints.
            # merge_strategy="review": the engine never merges - completed
            # tasks stay ready_for_merge with preserved worktrees and land in
            # the per-task review surface below.
            exit_code = int(
                runner(
                    paths=swarm_paths,
                    plan=plan,
                    cfg=cfg,
                    mode="auto",
                    yes=False,
                    max_steps=None,
                    api_key_override=None,
                    no_log=False,
                    parallel=parallel,
                    base_branch=None,
                    max_tasks=None,
                    max_attempts=None,
                    dry_run=False,
                    keep_worktrees=True,
                    retry_failed=False,
                    retry_changes_requested=False,
                    only=None,
                    retry_merge_conflicts=False,
                    review=False,
                    console=make_console(file=io.StringIO(), force_terminal=False, no_color=True),
                    trace_sink=BridgeSwarmTraceSink(session.surface),
                    trace_level="compact",
                    cancellation_event=job.cancellation_event,
                    worker_approval_handler=_worker_approval_router,
                    merge_strategy="review",
                )
                or 0
            )
            refreshed_plan = load_recorded_plan(record, migrate_legacy=False)
            harvest_ready_review_diffs(record, refreshed_plan)
            result = forge_swarm_result_payload(record, refreshed_plan, exit_code=exit_code)
            result["review"] = forge_swarm_review_result(record, refreshed_plan)
            cancelled = job.cancellation_event.is_set() or bool(result.get("interrupted"))
            _stop_heartbeat()
            if heartbeat_error:
                raise heartbeat_error[0]
            active_lease = _current_lease()
            if active_lease is None:
                raise SwarmLeaseLost("The durable swarm worker lease is unavailable.")
            durable_status = coordinator.get_status(
                session_id=session.session_id, job_id=job.job_id
            )
            if durable_status.state is SwarmJobState.CANCELLED:
                cancelled = True
            elif cancelled:
                _record_usage(active_lease)
                coordinator.interrupt(
                    active_lease,
                    error_code="swarm_interrupted",
                    error_summary=job.cancellation_reason or "The swarm run was interrupted.",
                )
            else:
                _record_usage(active_lease)
                coordinator.complete(active_lease, result=result)
            with self._state_lock:
                if cancelled:
                    _mark_job_completed(
                        job,
                        status="cancelled",
                        exit_code=exit_code,
                        error=job.cancellation_reason or "interrupted",
                        result=result,
                    )
                else:
                    _mark_job_completed(job, status="completed", exit_code=exit_code, result=result)
                _reconcile_session_job_state(session)
            session.surface.emit_status_update(mode=session.mode, model=getattr(cfg, "model", None))
            if cancelled:
                session.surface.emit_warning(
                    f"swarm_cancelled {record.plan_id} job_id={job.job_id} exit_code={exit_code}"
                )
            else:
                session.surface.emit_info(
                    f"swarm_completed {record.plan_id} job_id={job.job_id} "
                    f"status={result.get('run_status')} exit_code={exit_code}"
                )
        except BridgeCancellationError as e:
            _stop_heartbeat()
            message = str(redact_secrets(e.reason or "cancelled_by_user"))
            _interrupt_durable(error_code="swarm_interrupted", error_summary=message)
            with self._state_lock:
                _mark_job_completed(job, status="cancelled", exit_code=130, error=message)
                _reconcile_session_job_state(session)
            session.surface.emit_warning(
                f"swarm_cancelled {record.plan_id} job_id={job.job_id} reason={message}"
            )
        except ProtocolError as e:
            _stop_heartbeat()
            message = str(redact_secrets(e.message))
            _fail_durable(error_code="forge_swarm_failed", error_summary=message)
            with self._state_lock:
                job.error_code = e.code
                _mark_job_completed(job, status="failed", exit_code=1, error=message)
                _reconcile_session_job_state(session)
            session.surface.emit_error(e.code, message, False)
        except (ResumableSwarmError, Exception) as e:  # noqa: BLE001
            _stop_heartbeat()
            message = str(redact_secrets(f"Forge swarm run failed: {e}"))
            _fail_durable(error_code="forge_swarm_failed", error_summary=message)
            with self._state_lock:
                _mark_job_completed(job, status="failed", exit_code=1, error=message)
                _reconcile_session_job_state(session)
            session.surface.emit_error("forge_swarm_failed", message, False)
        finally:
            _stop_heartbeat()
            with lease_lock:
                job.durable_swarm_lease = None
            with self._state_lock:
                _reconcile_session_job_state(session)
                self._prune_terminal_jobs_locked()
            session.surface.with_job(None)

    def _swarm_job_context(
        self, request: ProtocolRequest
    ) -> tuple[
        BridgeSession,
        str,
        BridgeJob | None,
        DurableResumableSwarmCoordinator,
    ]:
        session_id = _required_str(request.params, "session_id", request_id=request.id)
        session = self._require_session(session_id, request_id=request.id)
        job_id = _required_str(request.params, "job_id", request_id=request.id)
        with self._state_lock:
            job = self._jobs.get(job_id)
        if job is not None and (job.kind != "forge_swarm" or job.session_id != session.session_id):
            raise ProtocolError(
                "invalid_job",
                "Job is not a Forge swarm job for the session.",
                request_id=request.id,
            )
        coordinator = self._resumable_swarm_for_session(session, request_id=request.id)
        return session, job_id, job, coordinator

    def _forge_swarm_status(self, request: ProtocolRequest) -> dict[str, Any]:
        session, job_id, job, coordinator = self._swarm_job_context(request)
        try:
            coordinator.recover_stale_jobs(session_id=session.session_id)
            durable = coordinator.get_status(session_id=session.session_id, job_id=job_id)
        except SwarmJobNotFound as exc:
            if job is None:
                raise ProtocolError("job_not_found", str(exc), request_id=request.id) from exc
            durable = None
        except ResumableSwarmError as exc:
            raise ProtocolError("resumable_swarm_error", str(exc), request_id=request.id) from exc
        if job is None:
            return {"session_id": session.session_id, **durable.public_payload()}
        payload = _job_summary(job)
        payload["cancellation_requested"] = job.cancellation_event.is_set()
        if durable is not None:
            payload["durable"] = durable.public_payload()
        record: ForgePlanRecord | None
        with self._state_lock:
            record = self._forge_plans.get(str(job.plan_id or ""))
        if record is not None:
            try:
                plan = load_recorded_plan(record, migrate_legacy=False)
            except ProtocolError:
                plan = None
            if plan is not None:
                counts: dict[str, int] = {}
                for task in plan.get("tasks") or []:
                    if not isinstance(task, dict):
                        continue
                    status = str(task.get("status") or "planned")
                    counts[status] = counts.get(status, 0) + 1
                payload["task_status_counts"] = counts
        return payload

    def _forge_swarm_job_result(self, request: ProtocolRequest) -> dict[str, Any]:
        job_id = _required_str(request.params, "job_id", request_id=request.id)
        session_id = _optional_str(request.params, "session_id", request_id=request.id)
        with self._state_lock:
            job = self._jobs.get(job_id)
        if job is not None and job.kind != "forge_swarm":
            raise ProtocolError(
                "invalid_job",
                "Job is not a Forge swarm job.",
                request_id=request.id,
            )
        if job is None:
            if session_id is None:
                raise ProtocolError(
                    "missing_field",
                    "session_id is required to recover a durable swarm result.",
                    request_id=request.id,
                )
            session = self._require_session(session_id, request_id=request.id)
            coordinator = self._resumable_swarm_for_session(session, request_id=request.id)
            try:
                coordinator.recover_stale_jobs(session_id=session.session_id)
                durable_result = coordinator.get_result(
                    session_id=session.session_id, job_id=job_id
                )
            except SwarmJobNotFound as exc:
                raise ProtocolError("job_not_found", str(exc), request_id=request.id) from exc
            except ResumableSwarmError as exc:
                raise ProtocolError(
                    "resumable_swarm_error", str(exc), request_id=request.id
                ) from exc
            status = durable_result.status
            if status.state is SwarmJobState.FAILED:
                raise ProtocolError(
                    status.error_code or "forge_swarm_failed",
                    status.error_summary or "Forge swarm job failed.",
                    request_id=request.id,
                )
            payload = {
                "session_id": session.session_id,
                **status.public_payload(),
                "status": (
                    "completed" if status.state is SwarmJobState.SUCCEEDED else status.state.value
                ),
                "complete": status.state not in {SwarmJobState.QUEUED, SwarmJobState.RUNNING},
            }
            if durable_result.result is not None:
                payload.update(durable_result.result)
            return payload
        if session_id is not None and session_id != job.session_id:
            raise ProtocolError(
                "invalid_job",
                "Job is not a Forge swarm job for the session.",
                request_id=request.id,
            )
        if job.status in ACTIVE_JOB_STATUSES:
            return {
                "session_id": job.session_id,
                "job_id": job.job_id,
                "plan_id": job.plan_id,
                "status": job.status,
                "state": job.status,
                "complete": False,
                "cancellable": bool(job.cancellable and job.status in {"queued", "running"}),
                "cancellation_requested": job.cancellation_event.is_set(),
            }
        if job.status == "cancelled":
            payload = {
                "session_id": job.session_id,
                "job_id": job.job_id,
                "plan_id": job.plan_id,
                "status": "cancelled",
                "state": "cancelled",
                "complete": True,
                "cancelled": True,
                "cancellation_reason": job.cancellation_reason,
                "job": _job_summary(job),
            }
            if job.result is not None:
                payload["result"] = job.result
            return payload
        if job.status == "failed":
            raise ProtocolError(
                job.error_code or "forge_swarm_failed",
                job.error or "Forge swarm job failed.",
                request_id=request.id,
            )
        if job.result is None:
            raise ProtocolError(
                "forge_swarm_failed",
                "Forge swarm job completed without a result.",
                request_id=request.id,
            )
        return {
            "job_id": job.job_id,
            "status": "completed",
            "state": "completed",
            "complete": True,
            "job": _job_summary(job),
            **job.result,
        }

    def _forge_swarm_cancel(self, request: ProtocolRequest) -> dict[str, Any]:
        session, job_id, job, coordinator = self._swarm_job_context(request)
        reason = (
            _optional_str(request.params, "reason", request_id=request.id) or "cancelled_by_user"
        )
        expected_revision = _optional_int(
            request.params.get("expected_revision"), request_id=request.id
        )
        try:
            durable = coordinator.cancel_job(
                session_id=session.session_id,
                job_id=job_id,
                expected_revision=expected_revision,
            )
        except SwarmJobNotFound as exc:
            if job is None:
                raise ProtocolError("job_not_found", str(exc), request_id=request.id) from exc
            durable = None
        except ResumableSwarmError as exc:
            raise ProtocolError("resumable_swarm_error", str(exc), request_id=request.id) from exc
        if job is None:
            return {
                "session_id": session.session_id,
                "job_id": job_id,
                "status": "cancelled",
                **durable.public_payload(),
            }
        with self._state_lock:
            result = _request_job_cancellation(job, reason)
            response = {
                "session_id": session.session_id,
                "plan_id": job.plan_id,
                "job_id": job.job_id,
                "status": result["status"],
                "state": result["state"],
                "job": _job_summary(job),
            }
            if durable is not None:
                response["durable"] = durable.public_payload()
        if result["status"] == "cancellation_requested":
            self._cancel_pending_approvals(session, reason)
            session.surface.emit_warning(
                f"swarm_cancellation_requested {job.job_id} reason={job.cancellation_reason}"
            )
        return response

    def _forge_swarm_reconcile(self, request: ProtocolRequest) -> dict[str, Any]:
        params = request.params
        action = (_optional_str(params, "action", request_id=request.id) or "report").lower()
        if action in {"harvest", "discard"}:
            self._require_workspace_trusted(request)
        if action == "discard":
            self._require_confirmation(request)
        session, record, plan = self._forge_request_context(request, migrate_legacy=False)
        if action != "report":
            self._require_no_active_swarm_job(session, request_id=request.id, action="reconcile")
        raw_task_ids = _optional_task_ids(params.get("task_ids"), request_id=request.id)
        base_branch = _optional_str(params, "base_branch", request_id=request.id)
        try:
            result = forge_swarm_reconcile_result(
                record,
                plan,
                action=action,
                task_ids=raw_task_ids or None,
                base_branch=base_branch,
            )
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        if action != "report":
            session.surface.emit_info(f"swarm_reconcile_{action} {record.plan_id}")
        return result

    def _require_no_active_swarm_job(
        self, session: BridgeSession, *, request_id: RequestId, action: str
    ) -> None:
        with self._state_lock:
            _reconcile_session_job_state(session)
            active = session.active_job
            if _job_is_active(active) and active is not None and active.kind == "forge_swarm":
                raise ProtocolError(
                    "swarm_job_active",
                    f"forge.swarm.{action} is unavailable while a swarm job is active "
                    "for the session.",
                    request_id=request_id,
                )

    def _forge_swarm_review(self, request: ProtocolRequest) -> dict[str, Any]:
        _session, record, plan = self._forge_request_context(request, migrate_legacy=False)
        try:
            return forge_swarm_review_result(record, plan)
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e

    def _forge_swarm_apply(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        session, record, plan = self._forge_request_context(request, migrate_legacy=False)
        self._require_no_active_swarm_job(session, request_id=request.id, action="apply")
        task_ids = _optional_task_ids(request.params.get("task_ids"), request_id=request.id)
        if not task_ids:
            raise ProtocolError(
                "missing_field",
                "forge.swarm.apply requires explicit task_ids.",
                request_id=request.id,
            )
        base_branch = _optional_str(request.params, "base_branch", request_id=request.id)
        try:
            result = forge_swarm_apply_result(
                record, plan, task_ids=task_ids, base_branch=base_branch
            )
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        for item in result.get("applied") or []:
            session.surface.emit_swarm_worker_state_changed(
                str(item.get("task_id") or ""), "merged", role="forge_swarm_review"
            )
        session.surface.emit_status_update(mode=session.mode)
        session.surface.emit_info(
            f"swarm_review_applied {record.plan_id} "
            f"tasks={','.join(str(item.get('task_id')) for item in result.get('applied') or [])}"
        )
        return result

    def _forge_swarm_discard(self, request: ProtocolRequest) -> dict[str, Any]:
        self._require_workspace_trusted(request)
        self._require_confirmation(request)
        session, record, plan = self._forge_request_context(request, migrate_legacy=False)
        self._require_no_active_swarm_job(session, request_id=request.id, action="discard")
        task_ids = _optional_task_ids(request.params.get("task_ids"), request_id=request.id)
        if not task_ids:
            raise ProtocolError(
                "missing_field",
                "forge.swarm.discard requires explicit task_ids.",
                request_id=request.id,
            )
        try:
            result = forge_swarm_discard_result(record, plan, task_ids=task_ids)
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        session.surface.emit_status_update(mode=session.mode)
        session.surface.emit_info(
            f"swarm_review_discarded {record.plan_id} "
            f"tasks={','.join(str(item.get('task_id')) for item in result.get('discarded') or [])}"
        )
        return result

    def _forge_review_start(
        self, request: ProtocolRequest
    ) -> tuple[dict[str, Any], Callable[[], None]]:
        self._require_workspace_trusted(request)
        cfg = clone_cfg(load_config())
        _apply_config_overrides(cfg, request.params, request_id=request.id)
        task_id = _required_str(request.params, "task_id", request_id=request.id)
        with self._state_lock:
            session, record, plan = self._forge_request_context(request, migrate_legacy=False)
            _reconcile_session_job_state(session)
            if _job_is_active(session.active_job):
                raise ProtocolError(
                    "session_busy",
                    "Session already has a running job.",
                    request_id=request.id,
                )
            job = BridgeJob(
                job_id=_make_job_id(),
                session_id=session.session_id,
                created_at=_now_iso(),
                kind="forge_review",
                plan_id=record.plan_id,
            )
            self._register_job_locked(session, job)

        def _start() -> None:
            thread = threading.Thread(
                target=self._run_forge_review_job,
                args=(session, job, record, plan, task_id, cfg),
                name=f"alysis-ide-forge-review-{job.job_id}",
                daemon=True,
            )
            self._start_job_thread(session, job, thread)

        return {
            "session_id": session.session_id,
            "plan_id": record.plan_id,
            "task_id": task_id,
            "job_id": job.job_id,
            "status": "started",
        }, _start

    def _run_forge_review_job(
        self,
        session: BridgeSession,
        job: BridgeJob,
        record: ForgePlanRecord,
        plan: dict[str, Any],
        task_id: str,
        cfg: Any,
    ) -> None:
        session.surface.with_job(job.job_id)
        try:
            with self._state_lock:
                _mark_job_running(job)
            session.surface.emit_info(
                f"forge_review_started {record.plan_id} task_id={task_id} job_id={job.job_id}"
            )
            # Cooperative checkpoints bracket the provider call; the review
            # engine itself is a single blocking request.
            _check_job_cancelled(job)
            result = forge_review_result(record, plan, task_id=task_id, cfg=cfg)
            _check_job_cancelled(job)
            with self._state_lock:
                _mark_job_completed(job, status="completed", exit_code=0, result=result)
                _reconcile_session_job_state(session)
            session.surface.emit_status_update(mode=session.mode, model=getattr(cfg, "model", None))
            session.surface.emit_info(f"forge_review_completed {record.plan_id} task_id={task_id}")
        except BridgeCancellationError as e:
            message = str(redact_secrets(e.reason or "cancelled_by_user"))
            with self._state_lock:
                _mark_job_completed(job, status="cancelled", exit_code=130, error=message)
                _reconcile_session_job_state(session)
            session.surface.emit_warning(f"forge_review_cancelled {job.job_id} reason={message}")
        except ProtocolError as e:
            message = str(redact_secrets(e.message))
            with self._state_lock:
                job.error_code = e.code
                _mark_job_completed(job, status="failed", exit_code=1, error=message)
                _reconcile_session_job_state(session)
            session.surface.emit_error(e.code, message, False)
        except Exception as e:  # noqa: BLE001
            message = str(redact_secrets(f"Forge review failed: {e}"))
            with self._state_lock:
                _mark_job_completed(job, status="failed", exit_code=1, error=message)
                _reconcile_session_job_state(session)
            session.surface.emit_error("forge_review_failed", message, False)
        finally:
            with self._state_lock:
                _reconcile_session_job_state(session)
                self._prune_terminal_jobs_locked()
            session.surface.with_job(None)

    def _forge_review_job_result(self, request: ProtocolRequest) -> dict[str, Any]:
        job_id = _required_str(request.params, "job_id", request_id=request.id)
        job = self._require_job(job_id, request_id=request.id)
        if job.kind != "forge_review":
            raise ProtocolError(
                "invalid_job",
                "Job is not a Forge review job.",
                request_id=request.id,
            )
        if job.status in ACTIVE_JOB_STATUSES:
            return {
                "session_id": job.session_id,
                "job_id": job.job_id,
                "plan_id": job.plan_id,
                "status": job.status,
                "state": job.status,
                "complete": False,
                "cancellable": bool(job.cancellable and job.status in {"queued", "running"}),
                "cancellation_requested": job.cancellation_event.is_set(),
            }
        if job.status == "cancelled":
            return {
                "session_id": job.session_id,
                "job_id": job.job_id,
                "plan_id": job.plan_id,
                "status": "cancelled",
                "state": "cancelled",
                "complete": True,
                "cancelled": True,
                "cancellation_reason": job.cancellation_reason,
                "job": _job_summary(job),
            }
        if job.status == "failed":
            raise ProtocolError(
                job.error_code or "forge_review_failed",
                job.error or "Forge review job failed.",
                request_id=request.id,
            )
        if job.result is None:
            raise ProtocolError(
                "forge_review_failed",
                "Forge review job completed without a result.",
                request_id=request.id,
            )
        return job.result

    def _diff_list(self, request: ProtocolRequest) -> dict[str, Any]:
        session_id = _required_str(request.params, "session_id", request_id=request.id)
        self._require_session(session_id, request_id=request.id)
        plan_id = _optional_str(request.params, "plan_id", request_id=request.id)
        if plan_id:
            records = [
                self._require_forge_plan(
                    session_id=session_id,
                    plan_id=plan_id,
                    request_id=request.id,
                )
            ]
        else:
            records = self._forge_records_for_session(session_id)
        return diff_list_result(records)

    def _diff_get(self, request: ProtocolRequest) -> dict[str, Any]:
        session_id = _required_str(request.params, "session_id", request_id=request.id)
        self._require_session(session_id, request_id=request.id)
        plan_id = _required_str(request.params, "plan_id", request_id=request.id)
        record = self._require_forge_plan(
            session_id=session_id,
            plan_id=plan_id,
            request_id=request.id,
        )
        diff_id = _required_str(request.params, "diff_id", request_id=request.id)
        try:
            return diff_get_result(
                [record],
                diff_id=diff_id,
                max_bytes=request.params.get("max_bytes"),
            )
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e

    def _request_host_action(
        self, session: BridgeSession, action: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if session.host_approval_protocol != 1:
            return self._exchange_host_action(session, action, arguments)
        prepared = self._exchange_host_action(session, action, arguments, phase="prepare")
        token = prepared.get("preparation_id")
        if not isinstance(token, str) or not re.fullmatch(r"pa_[a-f0-9]{32}", token):
            raise HostActionError(
                "invalid_host_preparation",
                "The host did not bind this action.",
                dispatch_state="not_dispatched",
            )
        for key in ("required", "write_effect"):
            if type(prepared.get(key)) is not bool:
                raise HostActionError(
                    "invalid_host_preparation",
                    "Invalid host effect descriptor.",
                    dispatch_state="not_dispatched",
                )
        context = None
        if "context" in prepared:
            try:
                context = normalize_connector_approval_context(prepared["context"])
            except ValueError as exc:
                raise HostActionError(
                    "invalid_host_preparation", str(exc), dispatch_state="not_dispatched"
                ) from exc
        if session.mode == "readonly" and prepared["write_effect"]:
            with suppress(HostActionError):
                self._exchange_host_action(
                    session, action, arguments, phase="discard", prepared_token=token
                )
            raise HostActionError(
                "approval_denied_by_policy",
                "This action is unavailable in Read only.",
                dispatch_state="not_dispatched",
            )
        request = ApprovalRequest(
            kind=str(prepared.get("kind", "host_action"))[:128],
            reason=str(prepared.get("reason", "This action needs authorization."))[:2000],
            preview=str(prepared.get("preview", action))[:10000],
            command=action,
            metadata={
                "access": "write" if prepared["write_effect"] else "read",
                "host_action": action,
                "target": str(prepared.get("target", ""))[:2000],
                "prepared_action": token,
                **({"connector_context": context} if context is not None else {}),
            },
        )
        try:
            decision = decide_action_approval(
                request,
                mode=session.mode,
                root=session.root,
                surface=session.surface,
                can_ask_person=True,
                reviewer=getattr(session.agent_session, "approval_reviewer", None),
                requires_authorization=prepared["required"] or prepared["write_effect"],
            )
        except Exception:
            with suppress(HostActionError):
                self._exchange_host_action(
                    session, action, arguments, phase="discard", prepared_token=token
                )
            raise
        if not decision.allow:
            with suppress(HostActionError):
                self._exchange_host_action(
                    session, action, arguments, phase="discard", prepared_token=token
                )
            state = decision.status or "denied"
            raise HostActionError(
                "approval_" + state,
                "This action was not dispatched: approval " + state + ".",
                dispatch_state="not_dispatched",
            )
        job = session.active_job
        if session.closed or job is None or job.cancellation_event.is_set():
            raise HostActionError(
                "host_action_cancelled",
                "The owning task changed before dispatch.",
                dispatch_state="not_dispatched",
            )
        return self._exchange_host_action(
            session,
            action,
            arguments,
            phase="execute",
            prepared_token=token,
            write_effect=prepared["write_effect"],
        )

    def _exchange_host_action(
        self,
        session: BridgeSession,
        action: str,
        arguments: dict[str, Any],
        *,
        phase: str = "execute",
        prepared_token: str | None = None,
        write_effect: bool | None = None,
    ) -> dict[str, Any]:
        normalized_action = str(action or "").strip()
        if not session.workspace_trusted:
            raise HostActionError(
                "host_action_workspace_untrusted",
                "IDE host actions are unavailable in an untrusted workspace.",
            )
        if normalized_action not in session.host_actions:
            raise HostActionError(
                "host_action_not_negotiated",
                "The IDE host did not negotiate this action for the owning session.",
            )
        normalized_arguments = normalize_host_action_arguments(normalized_action, arguments)
        if normalized_action == GOOGLE_MCP_ACTION:
            catalog = next(
                (c for c in session.google_mcp if c["service"] == normalized_arguments["service"]),
                None,
            )
            if catalog is None:
                raise HostActionError(
                    "google_plugin_inactive", "Google service was not negotiated."
                )
            assert_google_current(session.root, catalog)
            tool = next(
                (t for t in catalog["tools"] if t["name"] == normalized_arguments["name"]), None
            )
            if tool is None or (session.mode == "readonly" and not tool["readOnly"]):
                raise HostActionError(
                    "google_tool_unavailable", "Google tool is not enabled for this session."
                )
        if session.host_mcp is not None and (
            normalized_action in GITHUB_DISCOVERY_ACTIONS or normalized_action.startswith("github.")
        ):
            assert_host_mcp_current(session.root, session.host_mcp)
        if normalized_action in GITHUB_DISCOVERY_ACTIONS:
            if session.host_mcp is None:
                raise HostActionError(
                    "github_plugin_inactive", "The GitHub MCP plugin was not negotiated."
                )
            if session.host_mcp["protocol_version"] == 2:
                from ..mcp.github_discovery import discovery_arguments

                normalized_arguments = discovery_arguments(normalized_action, normalized_arguments)
            elif normalized_action == GITHUB_MCP_ACTION and set(normalized_arguments) != {
                "name",
                "arguments",
            }:
                raise HostActionError(
                    "invalid_github_mcp", "GitHub MCP v1 cannot execute a v2 call."
                )
        if normalized_action == GITHUB_MCP_ACTION and session.host_mcp["protocol_version"] == 1:
            tool = next(
                (
                    tool
                    for tool in session.host_mcp["tools"]
                    if tool["name"] == normalized_arguments["name"]
                ),
                None,
            )
            if tool is None or (session.mode == "readonly" and not tool["readOnly"]):
                raise HostActionError(
                    "github_mcp_tool_unavailable", "The MCP tool is not enabled for this session."
                )
        if json_size_bytes(normalized_arguments) > HOST_ACTION_MAX_ARGUMENT_BYTES:
            raise HostActionError(
                "host_action_arguments_too_large",
                "IDE host action arguments exceed the allowed size.",
            )

        with self._state_lock:
            _reconcile_session_job_state(session)
            job = session.active_job
            if session.closed or session.close_when_idle or not _job_is_active(job):
                raise HostActionError(
                    "host_action_session_inactive",
                    "IDE host actions require an active owning session job.",
                    dispatch_state="not_dispatched",
                )
            assert job is not None
            if job.cancellation_event.is_set():
                raise HostActionError(
                    "host_action_cancelled",
                    "The owning session job was cancelled before the IDE host action started.",
                    dispatch_state="not_dispatched",
                )

        # Old hosts cannot report a dispatch boundary. Classify their supported
        # actions conservatively so a timed-out mutation is never auto-retryable.
        effect = bool(write_effect)
        if session.host_approval_protocol == 0:
            effect = normalized_action not in {
                "tasks.list",
                "tasks.status",
                "debug.list",
                "debug.status",
                "github.repo.read",
                "github.pr.list",
                "mcp.github.discover",
                "mcp.github.describe",
                "terminal.list",
                "terminal.read",
                "connectors.status",
                "connectors.github.repositories",
                "browser.list",
                "browser.read",
                "browser.snapshot",
                "browser.screenshot",
                "browser.open",
                "browser.navigate",
                "browser.back",
                "browser.forward",
                "browser.scroll",
            }
            if normalized_action == GOOGLE_MCP_ACTION or (
                normalized_action == GITHUB_MCP_ACTION and session.host_mcp["protocol_version"] == 1
            ):
                effect = not tool["readOnly"]
        if (
            normalized_action in GITHUB_DISCOVERY_ACTIONS
            and session.host_mcp["protocol_version"] == 2
        ):
            with session.host_action_lock:
                session.github_discovery.begin(job.job_id)
                session.github_discovery.check(
                    normalized_action, normalized_arguments, readonly=session.mode == "readonly"
                )
                if normalized_action == GITHUB_MCP_ACTION:
                    loaded = session.github_discovery.loaded[
                        (normalized_arguments["target_id"], normalized_arguments["name"])
                    ]
                    effect = not loaded["readOnly"]

        host_action_id = f"ha_{uuid.uuid4().hex}"
        pending = PendingHostActionRecord(
            host_action_id=host_action_id,
            session_id=session.session_id,
            job_id=job.job_id,
            action=normalized_action,
            arguments=normalized_arguments,
            workspace_fence=session.workspace_fence,
            capability_fingerprint=session.host_capability_fingerprint,
            expires_at=_expires_at(self._host_action_timeout_seconds),
            deadline=time.monotonic() + self._host_action_timeout_seconds,
            write_effect=effect if phase == "execute" else False,
            phase=phase,
        )
        with session.host_action_lock:
            self._prune_resolved_host_actions_locked(session, time.monotonic())
            session.pending_host_actions[host_action_id] = pending

        session.surface.emit(
            ProtocolPayloadEvent(
                "host_action_requested",
                {
                    "protocol_version": HOST_ACTION_PROTOCOL_VERSION,
                    "host_action_id": pending.host_action_id,
                    "action": pending.action,
                    "arguments": _json_clone(pending.arguments),
                    "workspace_root": os.fspath(session.root),
                    "workspace_fence": pending.workspace_fence,
                    "capability_fingerprint": pending.capability_fingerprint,
                    "expires_at": pending.expires_at,
                    "max_result_bytes": HOST_ACTION_MAX_RESULT_BYTES,
                    "job_id": pending.job_id,
                    **(
                        {"phase": phase, "preparation_id": prepared_token}
                        if session.host_approval_protocol == 1
                        else {}
                    ),
                },
            )
        )

        cancellation_to_emit: tuple[PendingHostActionRecord, str] | None = None
        try:
            while not pending.done.wait(timeout=0.05):
                with session.host_action_lock:
                    if pending.resolved:
                        break
                    if job.cancellation_event.is_set():
                        reason = _bounded_host_action_reason(
                            job.cancellation_reason or "cancelled_by_user"
                        )
                        self._resolve_host_action_error_locked(
                            session,
                            pending,
                            HostActionError(
                                "host_action_cancelled",
                                "The IDE host action was cancelled with its owning session job.",
                                dispatch_state="dispatched"
                                if pending.dispatched
                                else "not_dispatched"
                                if session.host_approval_protocol == 1
                                else "unknown",
                            ),
                            status="cancelled",
                        )
                        cancellation_to_emit = (pending, reason)
                        break
                    if time.monotonic() > pending.deadline:
                        self._resolve_host_action_error_locked(
                            session,
                            pending,
                            HostActionError(
                                "host_action_timeout",
                                "The dispatched host write may have completed; inspect its outcome before retrying."
                                if pending.write_effect
                                and (pending.dispatched or session.host_approval_protocol != 1)
                                else "The IDE host action did not respond before its deadline.",
                                retryable=not pending.write_effect
                                or (session.host_approval_protocol == 1 and not pending.dispatched),
                                dispatch_state="dispatched"
                                if pending.dispatched
                                else "not_dispatched"
                                if session.host_approval_protocol == 1
                                else "unknown",
                            ),
                            status="expired",
                        )
                        cancellation_to_emit = (pending, "deadline_exceeded")
                        break

            if cancellation_to_emit is not None:
                self._emit_host_action_cancelled(session, *cancellation_to_emit)
            with session.host_action_lock:
                if pending.error is not None:
                    raise pending.error
                if pending.result is None:
                    raise HostActionError(
                        "host_action_failed",
                        "The IDE host action ended without a bounded result.",
                    )
                if (
                    normalized_action in GITHUB_DISCOVERY_ACTIONS
                    and session.host_mcp["protocol_version"] == 2
                ):
                    assert_host_mcp_current(session.root, session.host_mcp)
                    if (
                        job.cancellation_event.is_set()
                        or session.closed
                        or session.active_job is not job
                    ):
                        raise HostActionError(
                            "host_action_cancelled",
                            "The owning GitHub operation changed before result delivery.",
                        )
                    session.github_discovery.check(
                        normalized_action, normalized_arguments, readonly=session.mode == "readonly"
                    )
                    if phase == "execute" and normalized_action != GITHUB_MCP_ACTION:
                        session.github_discovery.record(
                            normalized_action,
                            normalized_arguments,
                            pending.result,
                            readonly=session.mode == "readonly",
                        )
                return _json_clone(pending.result)
        finally:
            with session.host_action_lock:
                session.pending_host_actions.pop(host_action_id, None)
                self._prune_resolved_host_actions_locked(session, time.monotonic())

    def _resolve_host_action_error_locked(
        self,
        session: BridgeSession,
        pending: PendingHostActionRecord,
        error: HostActionError,
        *,
        status: str,
    ) -> bool:
        if pending.resolved:
            return False
        pending.error = error
        pending.status = status
        pending.resolved = True
        session.resolved_host_actions[pending.host_action_id] = ResolvedHostActionRecord(
            status=status,
            resolved_at=time.monotonic(),
        )
        pending.done.set()
        return True

    def _emit_host_action_cancelled(
        self,
        session: BridgeSession,
        pending: PendingHostActionRecord,
        reason: str,
    ) -> None:
        session.surface.emit(
            ProtocolPayloadEvent(
                "host_action_cancelled",
                {
                    "protocol_version": HOST_ACTION_PROTOCOL_VERSION,
                    "host_action_id": pending.host_action_id,
                    "action": pending.action,
                    "workspace_fence": pending.workspace_fence,
                    "capability_fingerprint": pending.capability_fingerprint,
                    "reason": _bounded_host_action_reason(reason),
                },
            )
        )

    def _cancel_pending_host_actions(self, session: BridgeSession, reason: str) -> None:
        clean_reason = _bounded_host_action_reason(reason)
        cancelled: list[PendingHostActionRecord] = []
        with session.host_action_lock:
            for pending in list(session.pending_host_actions.values()):
                resolved = self._resolve_host_action_error_locked(
                    session,
                    pending,
                    HostActionError(
                        "host_action_cancelled",
                        "The IDE host action was cancelled with its owning session.",
                        dispatch_state="dispatched"
                        if pending.dispatched
                        else "not_dispatched"
                        if session.host_approval_protocol == 1
                        else "unknown",
                    ),
                    status="cancelled",
                )
                if resolved:
                    cancelled.append(pending)
        for pending in cancelled:
            self._emit_host_action_cancelled(session, pending, clean_reason)

    def _clear_session_host_actions(self, session: BridgeSession) -> None:
        self._cancel_pending_host_actions(session, "session_cleared")
        with session.host_action_lock:
            self._prune_resolved_host_actions_locked(session, time.monotonic())

    def _prune_resolved_host_actions_locked(
        self,
        session: BridgeSession,
        now: float,
    ) -> None:
        cutoff = now - HOST_ACTION_RESOLUTION_RETENTION_SECONDS
        for host_action_id, resolved in list(session.resolved_host_actions.items()):
            if resolved.resolved_at < cutoff:
                session.resolved_host_actions.pop(host_action_id, None)
        overflow = len(session.resolved_host_actions) - HOST_ACTION_RESOLUTION_MAX
        if overflow <= 0:
            return
        oldest = sorted(
            session.resolved_host_actions.items(),
            key=lambda item: item[1].resolved_at,
        )
        for host_action_id, _resolved in oldest[:overflow]:
            session.resolved_host_actions.pop(host_action_id, None)

    def _standing_approval(
        self,
        session: BridgeSession,
        approval_request: ApprovalRequest,
    ) -> StandingApproval | None:
        """What the permission rules and session grants decide, without showing a prompt.

        The same rules :meth:`_request_approval` applies before it shows the card, in the same
        order: a deny rule wins; credential files, folders outside the project, Computer Use
        and "ask" rules need a person; an allow rule or an earlier "allow for session" grant
        decides the rest. ``None`` means no rule applies (the reviewer may decide).
        """

        kind = _approval_kind(approval_request)
        metadata = approval_request.metadata if isinstance(approval_request.metadata, dict) else {}
        files = _normalized_files(approval_request.files)
        sensitive, external_directory = _permission_path_safety_flags(
            files, os.fspath(session.root)
        )
        try:
            evaluation = self._permission_policy_store.evaluate(
                PermissionRequest.create(
                    kind,
                    paths=files,
                    command=approval_request.command or approval_request.preview,
                    workspace_root=session.root,
                    sensitive=sensitive,
                    external_directory=external_directory,
                )
            )
        except PermissionPolicyError:
            return StandingApproval("deny", "the permission rules could not be read")
        if evaluation.decision is PolicyEffect.DENY:
            return StandingApproval("deny", "a permission rule denies this")
        if (
            kind == "computer_use"
            or sensitive
            or external_directory
            or metadata.get("mandatory_explicit_approval") is True
            or metadata.get("allow_for_session_disabled") is True
        ):
            return StandingApproval("ask_person", "this always needs your approval")
        if evaluation.decision is PolicyEffect.ASK and evaluation.matched_rule_id is not None:
            return StandingApproval("ask_person", "a permission rule asks every time")
        if evaluation.decision is PolicyEffect.ALLOW:
            return StandingApproval("allow", "a permission rule allows this")
        scope, scope_key, _warning = _approval_scope_for_request(kind, approval_request)
        if scope is None or scope_key is None or approval_request_is_destructive(approval_request):
            return None
        with session.approval_lock:
            self._prune_resolved_approvals_locked(session, time.monotonic())
            if _approved_scope_matches(session.approved_approval_scopes, kind, scope):
                return StandingApproval("allow", "you allowed this earlier in this session")
        return None

    def _request_approval(
        self,
        session: BridgeSession,
        approval_request: ApprovalRequest,
        emit_event: Callable[[Any], None],
        *,
        host_expires_at: datetime | None = None,
        host_deadline: float | None = None,
    ) -> ApprovalDecision:
        approval_id = uuid.uuid4().hex
        kind = _approval_kind(approval_request)
        timeout_seconds = (
            min(self._approval_timeout_seconds, MAX_COMPUTER_USE_APPROVAL_SECONDS)
            if kind == "computer_use"
            else self._approval_timeout_seconds
        )
        # Capture both boundaries before binding construction or UI emission. Never
        # restart this duration after elapsed preflight transport or a dialog.
        if host_deadline is not None and host_expires_at is None:
            raise ValueError("A host deadline requires its original UTC expiry.")
        approval_wall_time = datetime.now(UTC)
        approval_started = time.monotonic()
        expires_at = _expires_at(timeout_seconds)
        if host_expires_at is not None:
            if kind != "computer_use" or host_expires_at.tzinfo != UTC or host_deadline is None:
                raise ValueError("Host expiry is limited to trusted Computer Use approvals.")
            timeout_seconds = max(
                0.0,
                min(
                    timeout_seconds,
                    host_deadline - approval_started,
                    (host_expires_at - approval_wall_time).total_seconds(),
                ),
            )
            expires_at = (
                (approval_wall_time + timedelta(seconds=timeout_seconds))
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        scope, scope_key, scope_warning = _approval_scope_for_request(kind, approval_request)
        allow_for_session_supported = scope is not None and scope_key is not None
        context = session.surface.context
        request_metadata = (
            approval_request.metadata if isinstance(approval_request.metadata, dict) else {}
        )
        current_tool_call_id = getattr(session.surface, "current_tool_call_id", None)
        tool_call_id = current_tool_call_id() if callable(current_tool_call_id) else None
        tool_call_id = (
            tool_call_id
            or str(
                request_metadata.get("tool_call_id") or request_metadata.get("call_id") or ""
            ).strip()
            or None
        )
        active_job_id = session.active_job.job_id if session.active_job is not None else None
        job_id = context.job_id or active_job_id
        operation_id = (
            context.run_id or str(request_metadata.get("operation_id") or "").strip() or job_id
        )
        binding = build_approval_binding(
            approval_request,
            project_root=session.root,
            runtime_fingerprint=self._runtime_fingerprint,
            mode=session.mode,
            operation_id=operation_id,
            job_id=job_id,
            tool_call_id=tool_call_id,
            allow_for_session_supported=allow_for_session_supported,
        )
        if binding.destructive and allow_for_session_supported:
            scope = None
            scope_key = None
            allow_for_session_supported = False
            scope_warning = (
                "Allow for session is unavailable because destructive actions always require "
                "a fresh explicit decision."
            )
            binding = build_approval_binding(
                approval_request,
                project_root=session.root,
                runtime_fingerprint=self._runtime_fingerprint,
                mode=session.mode,
                operation_id=operation_id,
                job_id=job_id,
                tool_call_id=tool_call_id,
                allow_for_session_supported=False,
            )
        # Swarm-worker approvals are persistent: a paused task waits for an
        # explicit decision (or run cancellation) and is NEVER auto-denied on
        # timeout - the task simply stays paused at its checkpoint while
        # sibling workers continue.
        swarm_task_id = (
            str((approval_request.metadata or {}).get("forge_swarm_task_id") or "").strip() or None
        )
        persistent = swarm_task_id is not None and host_expires_at is None
        pending = PendingApprovalRecord(
            approval_id=approval_id,
            session_id=session.session_id,
            kind=kind,
            reason=str(approval_request.reason or ""),
            preview=str(approval_request.preview or ""),
            files=_normalized_files(approval_request.files),
            command=str(approval_request.command) if approval_request.command is not None else None,
            metadata=_json_object(approval_request.metadata),
            scope=scope,
            scope_key=scope_key,
            allow_for_session_supported=allow_for_session_supported,
            allow_for_session_warning=None if allow_for_session_supported else scope_warning,
            expires_at=None
            if persistent
            else expires_at
            if host_expires_at is not None
            else _expires_at(timeout_seconds),
            deadline=(
                min(host_deadline, approval_started + timeout_seconds)
                if host_deadline is not None
                else float("inf")
                if persistent
                else time.monotonic() + timeout_seconds
            ),
            binding=binding,
            host_expires_at=host_expires_at,
        )
        mandatory_explicit_approval = (
            pending.metadata.get("mandatory_explicit_approval") is True
            or pending.metadata.get("allow_for_session_disabled") is True
        )

        sensitive, external_directory = _permission_path_safety_flags(
            pending.files, os.fspath(session.root)
        )
        mandatory_explicit_approval = mandatory_explicit_approval or sensitive or external_directory
        if mandatory_explicit_approval:
            pending.allow_for_session_supported = False
            pending.allow_for_session_warning = "This operation requires one-time approval."
            pending.metadata["allow_for_session_disabled"] = True
        try:
            policy_evaluation = self._permission_policy_store.evaluate(
                PermissionRequest.create(
                    kind,
                    paths=pending.files,
                    command=pending.command or pending.preview,
                    workspace_root=session.root,
                    sensitive=sensitive,
                    external_directory=external_directory,
                )
            )
        except PermissionPolicyError:
            policy_evaluation = PermissionEvaluation(
                decision=PolicyEffect.DENY,
                reason="permission_policy_error",
                matched_rule_id="override:policy_error",
                matched_rule_source="builtin_safety",
                specificity=2_147_483_647,
            )

        if (
            policy_evaluation.decision is PolicyEffect.ASK
            and policy_evaluation.matched_rule_id is not None
        ):
            pending.allow_for_session_supported = False
            pending.allow_for_session_warning = (
                pending.allow_for_session_warning
                or "The permission rule requires approval each time."
            )
            pending.metadata["allow_for_session_disabled"] = True

        # The response must bind the policy the user actually sees, including
        # one-time safety and explicit ask rules applied above.
        if pending.allow_for_session_supported != allow_for_session_supported:
            pending.binding = build_approval_binding(
                approval_request,
                project_root=session.root,
                runtime_fingerprint=self._runtime_fingerprint,
                mode=session.mode,
                operation_id=operation_id,
                job_id=job_id,
                tool_call_id=tool_call_id,
                allow_for_session_supported=pending.allow_for_session_supported,
            )

        if policy_evaluation.decision is PolicyEffect.DENY:
            decision = ApprovalDecision(allow=False, allow_for_session=False)
            pending.decision = decision
            pending.result_status = "denied_by_policy"
            emit_event(
                InfoEmitted(
                    message=(
                        "approval_auto_denied "
                        f"approval_id={approval_id} reason={policy_evaluation.reason}"
                    )
                )
            )
            emit_event(_approval_result_event(pending, decision, "denied_by_policy"))
            return decision

        # Persistent allow rules predate and know nothing about host screen control;
        # a Computer Use action always needs a person (deny rules still apply above).
        if (
            not mandatory_explicit_approval
            and kind != "computer_use"
            and policy_evaluation is not None
            and policy_evaluation.decision is PolicyEffect.ALLOW
        ):
            emit_event(
                InfoEmitted(
                    message=(
                        "approval_auto_allowed "
                        f"approval_id={approval_id} reason=persistent_permission_rule"
                    )
                )
            )
            return ApprovalDecision(allow=True, allow_for_session=False)

        if host_expires_at is not None and _approval_deadline_expired(pending):
            return ApprovalDecision(allow=False, status="expired")
        auto_allowed = False
        with session.approval_lock:
            self._prune_resolved_approvals_locked(session, time.monotonic())
            if (
                pending.allow_for_session_supported
                and policy_evaluation.matched_rule_id is None
                and _approved_scope_matches(session.approved_approval_scopes, kind, scope)
            ):
                auto_allowed = True
            else:
                session.pending_approvals[approval_id] = pending

        if auto_allowed:
            if host_expires_at is not None and _approval_deadline_expired(pending):
                return ApprovalDecision(allow=False, status="expired")
            emit_event(
                InfoEmitted(
                    message=(
                        "approval_auto_allowed "
                        f"approval_id={approval_id} reason=allow_for_session_match"
                        + (f" task_id={swarm_task_id}" if swarm_task_id else "")
                    )
                )
            )
            return ApprovalDecision(allow=True, allow_for_session=True)

        if swarm_task_id:
            session.surface.emit_swarm_worker_state_changed(
                swarm_task_id, "approval_pending", role="forge_swarm"
            )
            session.surface.emit_info(
                f"swarm_approval_requested task_id={swarm_task_id} "
                f"approval_id={approval_id} kind={kind}"
            )
        emit_event(_approval_prompt_event(pending))

        try:
            while not pending.done.wait(
                timeout=timeout_seconds
                if host_expires_at is None
                else max(0.0, pending.deadline - time.monotonic())
            ):
                if persistent:
                    if swarm_task_id:
                        session.surface.emit_swarm_worker_state_changed(
                            swarm_task_id, "approval_pending", role="forge_swarm"
                        )
                        session.surface.emit_warning(
                            f"swarm_approval_still_pending task_id={swarm_task_id} "
                            f"approval_id={approval_id}"
                        )
                    continue
                with session.approval_lock:
                    if not pending.resolved:
                        self._expire_pending_approval_locked(session, pending)
                break

            with session.approval_lock:
                if (
                    host_expires_at is not None
                    and pending.decision is not None
                    and pending.decision.allow
                    and _approval_deadline_expired(pending)
                ):
                    self._expire_pending_approval_locked(session, pending)
                decision = pending.decision or ApprovalDecision(allow=False)
                result_status = pending.result_status or ("allowed" if decision.allow else "denied")
                # _resolve_approval records a session grant exactly once. Adding it
                # again here could resurrect a grant revoked before this waiter ran.
            emit_event(_approval_result_event(pending, decision, result_status))
            if swarm_task_id:
                session.surface.emit_swarm_worker_state_changed(
                    swarm_task_id, "started", role="forge_swarm"
                )
                session.surface.emit_info(
                    f"swarm_approval_resolved task_id={swarm_task_id} "
                    f"approval_id={approval_id} status={result_status}"
                )
            return replace(decision, status=result_status)
        finally:
            with session.approval_lock:
                session.pending_approvals.pop(approval_id, None)
                self._prune_resolved_approvals_locked(session, time.monotonic())

    def _resolve_approval(
        self,
        *,
        session: BridgeSession,
        approval_id: str,
        allow: bool,
        allow_for_session: bool,
        response_bindings: dict[str, Any],
        request_id: RequestId,
    ) -> dict[str, Any]:
        now = time.monotonic()
        with session.approval_lock:
            self._prune_resolved_approvals_locked(session, now)
            pending = session.pending_approvals.get(approval_id)
            if pending is None:
                resolved = session.resolved_approvals.get(approval_id)
                if resolved is not None:
                    if resolved.duplicate_error:
                        raise ProtocolError(
                            "duplicate_response",
                            "Approval id was already resolved by a previous response.",
                            request_id=request_id,
                        )
                    return _json_clone(resolved.response)
                raise ProtocolError(
                    "unknown_approval",
                    "No pending approval exists with that id.",
                    request_id=request_id,
                )

            if pending.resolved:
                if pending.response is not None and pending.response.get("status") == "expired":
                    return _json_clone(pending.response)
                raise ProtocolError(
                    "duplicate_response",
                    "Approval id was already resolved by a previous response.",
                    request_id=request_id,
                )

            if (
                _approval_deadline_expired(pending, now)
                if pending.host_expires_at is not None
                else now > pending.deadline
            ):
                self._expire_pending_approval_locked(session, pending)
                return _json_clone(pending.response or _approval_response(pending, "expired"))

            if pending.binding is not None:
                current_policy_fingerprint = approval_policy_fingerprint(
                    mode=session.mode,
                    kind=pending.kind,
                    destructive=pending.binding.destructive,
                    allow_for_session_supported=pending.allow_for_session_supported,
                )
                if current_policy_fingerprint != pending.binding.policy_fingerprint:
                    self._invalidate_pending_approval_locked(
                        session,
                        pending,
                        status="stale",
                    )
                    raise ProtocolError(
                        "stale_approval",
                        "Approval policy changed while the action was waiting.",
                        request_id=request_id,
                    )
                _validate_optional_approval_bindings(
                    response_bindings,
                    pending,
                    request_id=request_id,
                )

            requested_session_allow = bool(allow_for_session)
            effective_session_allow = (
                bool(allow)
                and requested_session_allow
                and pending.allow_for_session_supported
                and pending.scope is not None
                and pending.scope_key is not None
            )
            warning = pending.allow_for_session_warning
            if requested_session_allow and not effective_session_allow:
                warning = warning or (
                    "Allow for session was downgraded to allow once because this approval "
                    "does not have an exact session-safe scope."
                )
                pending.allow_for_session_warning = warning

            decision = ApprovalDecision(
                allow=bool(allow),
                allow_for_session=effective_session_allow,
            )
            pending.decision = decision
            pending.result_status = "allowed" if decision.allow else "denied"
            pending.response = _approval_response(pending, "applied", decision=decision)
            pending.resolved = True
            if decision.allow and decision.allow_for_session:
                self._remember_approval_scope_locked(session, pending)
            session.resolved_approvals[approval_id] = ResolvedApprovalRecord(
                response=_json_clone(pending.response),
                resolved_at=now,
                duplicate_error=True,
            )
            pending.done.set()
            return _json_clone(pending.response)

    def _expire_pending_approval_locked(
        self,
        session: BridgeSession,
        pending: PendingApprovalRecord,
    ) -> None:
        decision = ApprovalDecision(allow=False, allow_for_session=False)
        pending.decision = decision
        pending.result_status = "expired"
        pending.response = _approval_response(pending, "expired", decision=decision)
        pending.resolved = True
        session.resolved_approvals[pending.approval_id] = ResolvedApprovalRecord(
            response=_json_clone(pending.response),
            resolved_at=time.monotonic(),
            duplicate_error=False,
        )
        pending.done.set()

    def _invalidate_pending_approval_locked(
        self,
        session: BridgeSession,
        pending: PendingApprovalRecord,
        *,
        status: str,
    ) -> None:
        decision = ApprovalDecision(allow=False, allow_for_session=False)
        pending.decision = decision
        pending.result_status = status
        pending.response = _approval_response(pending, status, decision=decision)
        pending.resolved = True
        session.resolved_approvals[pending.approval_id] = ResolvedApprovalRecord(
            response=_json_clone(pending.response),
            resolved_at=time.monotonic(),
            duplicate_error=False,
        )
        pending.done.set()

    def _remember_approval_scope_locked(
        self,
        session: BridgeSession,
        pending: PendingApprovalRecord,
    ) -> None:
        if pending.scope is None or pending.scope_key is None:
            return
        if any(record.key == pending.scope_key for record in session.approved_approval_scopes):
            return
        session.approved_approval_scopes.append(
            ApprovalScopeRecord(kind=pending.kind, scope=pending.scope, key=pending.scope_key)
        )

    def _prune_resolved_approvals_locked(self, session: BridgeSession, now: float) -> None:
        cutoff = now - APPROVAL_RESOLUTION_RETENTION_SECONDS
        for approval_id, resolved in list(session.resolved_approvals.items()):
            if resolved.resolved_at < cutoff:
                session.resolved_approvals.pop(approval_id, None)
        overflow = len(session.resolved_approvals) - APPROVAL_RESOLUTION_MAX
        if overflow <= 0:
            return
        oldest = sorted(
            session.resolved_approvals.items(),
            key=lambda item: item[1].resolved_at,
        )
        for approval_id, _resolved in oldest[:overflow]:
            session.resolved_approvals.pop(approval_id, None)

    def _clear_session_approvals(self, session: BridgeSession) -> None:
        with session.approval_lock:
            for pending in list(session.pending_approvals.values()):
                if not pending.resolved:
                    self._expire_pending_approval_locked(session, pending)
            session.pending_approvals.clear()
            session.approved_approval_scopes.clear()
            session.resolved_approvals.clear()

    def _cancel_pending_approvals(self, session: BridgeSession, reason: str) -> None:
        clean_reason = str(redact_secrets(reason or "cancelled_by_user"))
        with session.approval_lock:
            for pending in list(session.pending_approvals.values()):
                if pending.resolved:
                    continue
                decision = ApprovalDecision(allow=False, allow_for_session=False)
                pending.decision = decision
                pending.result_status = f"cancelled:{clean_reason}"
                pending.response = _approval_response(pending, "cancelled", decision=decision)
                pending.resolved = True
                session.resolved_approvals[pending.approval_id] = ResolvedApprovalRecord(
                    response=_json_clone(pending.response),
                    resolved_at=time.monotonic(),
                    duplicate_error=False,
                )
                pending.done.set()
            session.pending_approvals.clear()

    def _cancel_pending_computer_use(self, session: BridgeSession, reason: str) -> None:
        clean_reason = str(redact_secrets(reason or "cancelled_by_user"))[:1000]
        with session.computer_use_lock:
            for pending in list(session.pending_computer_use.values()):
                if pending.resolved:
                    continue
                pending.response = {
                    "error": "computer_use_cancelled",
                    "message": clean_reason or "cancelled_by_user",
                }
                pending.resolved = True
                pending.done.set()

    def _require_session_for_approval(
        self,
        session_id: str,
        *,
        request_id: RequestId,
    ) -> BridgeSession:
        try:
            return self._require_session(session_id, request_id=request_id)
        except ProtocolError as e:
            if e.code == "invalid_session_id":
                raise ProtocolError("invalid_request", e.message, request_id=request_id) from e
            raise

    def _require_session(
        self,
        session_id: str,
        *,
        request_id: RequestId,
        allow_closing: bool = False,
    ) -> BridgeSession:
        clean = session_id.strip()
        _validate_session_id(clean, request_id=request_id)
        with self._state_lock:
            session = self._sessions.get(clean)
        if session is None or session.closed:
            raise ProtocolError(
                "session_not_found", "Session was not found.", request_id=request_id
            )
        if session.close_when_idle and not allow_closing:
            raise ProtocolError(
                "session_closing",
                "Session cancellation is settling and owned resources are closing.",
                request_id=request_id,
            )
        return session

    def _register_job_locked(self, session: BridgeSession, job: BridgeJob) -> None:
        self._prune_terminal_jobs_locked(reserve=1)
        from ..user_questions import question_broker

        questions = question_broker(session.surface)
        if questions is not None:
            questions.reset()
        session.active_job = job
        self._jobs[job.job_id] = job

    def _attach_durable_forge_job_locked(
        self,
        session: BridgeSession,
        record: ForgeRequestRecord,
    ) -> BridgeJob:
        """Attach durable status without replacing a live in-process worker."""

        status = _durable_forge_protocol_status(record.state)
        existing = self._jobs.get(record.job_id)
        if existing is not None:
            if record.state in {
                ForgeRequestState.COMPLETED,
                ForgeRequestState.FAILED,
                ForgeRequestState.CANCELLED,
                ForgeRequestState.INDETERMINATE,
            }:
                existing.status = status
                existing.updated_at = _iso_from_epoch(record.updated_at)
                existing.completed_at = _iso_from_epoch(record.terminal_at)
                existing.plan_id = record.plan_id
                existing.cancellable = False
                existing.error_code = record.error_code
                if record.state is ForgeRequestState.INDETERMINATE:
                    existing.error = "forge_plan_indeterminate"
                    existing.exit_code = 1
                elif record.state is ForgeRequestState.FAILED and not existing.error:
                    existing.error = "Forge Plan job failed."
                    existing.exit_code = 1
            _reconcile_session_job_state(session)
            return existing

        job = BridgeJob(
            job_id=record.job_id,
            session_id=session.session_id,
            created_at=_iso_from_epoch(record.created_at) or _now_iso(),
            kind="forge_plan",
            status=status,
            started_at=_iso_from_epoch(record.started_at),
            updated_at=_iso_from_epoch(record.updated_at),
            completed_at=_iso_from_epoch(record.terminal_at),
            exit_code=(
                1
                if record.state in {ForgeRequestState.FAILED, ForgeRequestState.INDETERMINATE}
                else 130
                if record.state is ForgeRequestState.CANCELLED
                else None
            ),
            error=(
                "forge_plan_indeterminate"
                if record.state is ForgeRequestState.INDETERMINATE
                else "Forge Plan job failed."
                if record.state is ForgeRequestState.FAILED
                else None
            ),
            error_code=record.error_code,
            plan_id=record.plan_id,
            cancellable=record.state in {ForgeRequestState.QUEUED, ForgeRequestState.RUNNING},
        )
        self._prune_terminal_jobs_locked(reserve=1)
        self._jobs[job.job_id] = job
        if record.state in {ForgeRequestState.QUEUED, ForgeRequestState.RUNNING}:
            session.active_job = job
        else:
            session.last_job = job
        return job

    def _reconstruct_durable_forge_plan_result(
        self,
        durable: ForgeRequestRecord,
        *,
        request_id: RequestId,
    ) -> dict[str, Any]:
        session: BridgeSession | None
        with self._state_lock:
            session = self._sessions.get(durable.session_id)
            if session is not None and session.closed:
                session = None
        created_session = session is None
        if session is None:
            try:
                binding = _resolve_workspace(durable.workspace_root, request_id=request_id)
            except ProtocolError:
                raise
            session = self._create_plan_inspection_session(binding.workspace_context.workspace_root)
        elif session.root.resolve() != durable.workspace_root.resolve():
            raise ProtocolError(
                "forge_plan_durability_error",
                "Durable Forge plan workspace binding does not match the attached session.",
                request_id=request_id,
            )
        if created_session:
            try:
                durable = self._forge_request_ledger.attach(
                    job_id=durable.job_id,
                    session_id=session.session_id,
                    workspace_root=session.root,
                )
            except ForgeRequestLedgerError as exc:
                self._close_session(session)
                raise ProtocolError(
                    "forge_plan_durability_error", str(exc), request_id=request_id
                ) from exc
        try:
            opened, plan = open_persisted_forge_plan(
                workspace_root=session.root,
                session_id=session.session_id,
                plan_id=str(durable.plan_id),
                source="durable_idempotent_recovery",
            )
        except ProtocolError as exc:
            if created_session:
                self._close_session(session)
            raise ProtocolError(exc.code, exc.message, request_id=request_id) from exc
        record = ForgePlanRecord(
            session_id=opened.session_id,
            plan_id=opened.plan_id,
            paths=opened.paths,
            status="planned",
            job_id=durable.job_id,
            source=opened.source,
            warnings=opened.warnings,
            verification_commands=opened.verification_commands,
        )
        result = forge_plan_result(record, plan, created_session=created_session)
        with self._state_lock:
            self._forge_plans[record.plan_id] = record
            session.artifact_store.add_root(
                ArtifactRoot(
                    forge_artifact_root_name(record.plan_id),
                    validate_forge_run_artifact_root(record),
                )
            )
            job = self._attach_durable_forge_job_locked(session, durable)
            job.result = result
        return result

    def _prune_terminal_jobs_locked(self, *, reserve: int = 0) -> None:
        target = max(0, self._job_history_max - max(0, reserve))
        if len(self._jobs) <= target:
            return
        for job_id, job in list(self._jobs.items()):
            if len(self._jobs) <= target:
                break
            if job.status in FINAL_JOB_STATUSES:
                self._jobs.pop(job_id, None)

    def _remove_session_locked(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._event_buffers.pop(session_id, None)
        self._event_dropped_counts.pop(session_id, None)
        self._trace_view_floors.pop(session_id, None)
        for plan_id, record in list(self._forge_plans.items()):
            if record.session_id == session_id:
                self._forge_plans.pop(plan_id, None)
        for job_id, job in list(self._jobs.items()):
            if job.session_id == session_id:
                self._jobs.pop(job_id, None)

    def _prune_closed_sessions_locked(self) -> None:
        closed_ids = [
            session_id for session_id, session in self._sessions.items() if session.closed
        ]
        excess = max(0, len(closed_ids) - self._closed_session_history_max)
        for session_id in closed_ids[:excess]:
            self._remove_session_locked(session_id)

    def _require_job(self, job_id: str, *, request_id: RequestId) -> BridgeJob:
        with self._state_lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise ProtocolError("job_not_found", "Job was not found.", request_id=request_id)
        return job

    def _create_plan_inspection_session(self, root: Path) -> BridgeSession:
        session_id = make_session_id()
        with self._state_lock:
            self._event_buffers.setdefault(session_id, deque(maxlen=self._event_replay_max))
        session_ref: dict[str, BridgeSession] = {}
        surface = ProtocolEventSurface(
            context=EventContext(session_id=session_id),
            emit=self._record_and_write_event,
            sequencer=EventSequencer(),
            approval_handler=lambda approval_request, emit_event: self._request_approval(
                session_ref["session"],
                approval_request,
                emit_event,
            ),
            standing_approval_handler=lambda approval_request: self._standing_approval(
                session_ref["session"],
                approval_request,
            ),
            semantic_activity_events=True,
        )
        session = BridgeSession(
            session_id=session_id,
            root=root,
            mode="readonly",
            surface=surface,
            agent_session=_PlanInspectionSession(),
            artifact_store=ArtifactStore([]),
        )
        session_ref["session"] = session
        with self._state_lock:
            self._sessions[session_id] = session
        return session

    def _close_session(self, session: BridgeSession) -> None:
        with session.close_lock:
            with self._state_lock:
                if session.closed:
                    return
                # One-shot host-media authority is never retained past a close
                # request, even when owned-resource cleanup must be retried.
                for retained_job in (session.active_job, session.last_job):
                    if retained_job is not None:
                        retained_job.host_image_binding = None
                        retained_job.prepared_host_image = None
            if session.task_budget_authority is not None:
                session.task_budget_authority.close()
            if session.task_budget_channel is not None:
                session.task_budget_channel.close()
            self._clear_session_approvals(session)
            self._clear_session_host_actions(session)
            self._cancel_pending_computer_use(session, "session_closed")
            with self._state_lock:
                for plan_id, record in list(self._forge_plans.items()):
                    if record.session_id == session.session_id:
                        self._forge_plans.pop(plan_id, None)
            if session.managed_browser is not None:
                try:
                    session.managed_browser.close_all()
                except Exception as exc:
                    raise BridgeSessionCleanupError(
                        "Managed browser cleanup is incomplete; retry session close."
                    ) from exc
            try:
                session.agent_session.close()
            except Exception as exc:
                raise BridgeSessionCleanupError(
                    "Agent session cleanup is incomplete; retry session close."
                ) from exc
            finally:
                _clear_bridge_host_screen_redactor(session)
            with self._state_lock:
                session.closed = True
            if session.host_actions:
                with suppress(Exception):
                    session.surface.emit(
                        ProtocolPayloadEvent(
                            "session_closed",
                            {
                                "protocol_version": HOST_ACTION_PROTOCOL_VERSION,
                                "workspace_fence": session.workspace_fence,
                                "capability_fingerprint": session.host_capability_fingerprint,
                            },
                        )
                    )
            with self._state_lock:
                self._prune_closed_sessions_locked()

    def _require_forge_plan(
        self,
        *,
        session_id: str,
        plan_id: str,
        request_id: RequestId,
    ) -> ForgePlanRecord:
        session = self._require_session(session_id, request_id=request_id)
        with self._state_lock:
            record = self._forge_plans.get(plan_id)
        if record is not None and record.session_id == session_id:
            return record
        try:
            record, _plan = open_persisted_forge_plan(
                workspace_root=session.root,
                session_id=session.session_id,
                plan_id=plan_id,
            )
        except ProtocolError:
            raise ProtocolError(
                "forge_plan_not_found",
                "Forge plan was not found for the session.",
                request_id=request_id,
            ) from None
        with self._state_lock:
            self._forge_plans[record.plan_id] = record
            session.artifact_store.add_root(
                ArtifactRoot(
                    forge_artifact_root_name(record.plan_id),
                    validate_forge_run_artifact_root(record),
                )
            )
        return record

    def _forge_request_context(
        self,
        request: ProtocolRequest,
        *,
        migrate_legacy: bool,
    ) -> tuple[BridgeSession, ForgePlanRecord, dict[str, Any]]:
        session_id = _required_str(request.params, "session_id", request_id=request.id)
        session = self._require_session(session_id, request_id=request.id)
        plan_id = _required_str(request.params, "plan_id", request_id=request.id)
        record = self._require_forge_plan(
            session_id=session_id,
            plan_id=plan_id,
            request_id=request.id,
        )
        try:
            plan = load_recorded_plan(record, migrate_legacy=migrate_legacy)
        except ProtocolError as e:
            raise ProtocolError(e.code, e.message, request_id=request.id) from e
        return session, record, plan

    def _require_workspace_trusted(self, request: ProtocolRequest) -> None:
        trusted = _optional_bool_or_none(
            request.params,
            "workspace_trusted",
            request_id=request.id,
        )
        if trusted is not True:
            raise ProtocolError(
                "workspace_trust_required",
                "This IDE method mutates workspace-local Alysis Code state and requires workspace_trusted=true.",
                request_id=request.id,
            )

    def _require_confirmation(self, request: ProtocolRequest) -> None:
        yes = _optional_bool(request.params, "yes", default=False, request_id=request.id)
        confirm = _optional_bool(
            request.params,
            "confirm",
            default=False,
            request_id=request.id,
        )
        if not yes and not confirm:
            raise ProtocolError(
                "confirmation_required",
                "This Forge method requires yes=true or confirm=true.",
                request_id=request.id,
            )

    def _forge_records_for_session(self, session_id: str) -> list[ForgePlanRecord]:
        with self._state_lock:
            return [
                record for record in self._forge_plans.values() if record.session_id == session_id
            ]

    def _trace_visible_events_locked(self, session_id: str) -> list[dict[str, Any]]:
        floor = self._trace_view_floors.get(session_id, 0)
        retained = self._event_buffers.get(session_id, ())
        return [event for event in retained if int(event.get("sequence") or 0) > floor]

    def _buffer_event_locked(self, clean_payload: dict[str, Any]) -> None:
        session_id = str(clean_payload.get("session_id") or "")
        if not session_id:
            return
        buffer = self._event_buffers.setdefault(session_id, deque(maxlen=self._event_replay_max))
        dropped_now = len(buffer) >= self._event_replay_max
        if dropped_now:
            self._event_dropped_counts[session_id] = (
                self._event_dropped_counts.get(session_id, 0) + 1
            )
        buffer.append(_json_clone(clean_payload))
        job_id = str(clean_payload.get("job_id") or "")
        if job_id:
            job = self._jobs.get(job_id)
            if job is not None:
                job.event_count += 1
                if dropped_now:
                    job.dropped_event_count += 1
                job.updated_at = _now_iso()

    def _record_and_write_event(self, payload: dict[str, Any]) -> None:
        raw_session_id = str(payload.get("session_id") or "")
        raw_job_id = str(payload.get("job_id") or "")
        with self._state_lock:
            session = self._sessions.get(raw_session_id)
            payload_job = self._jobs.get(raw_job_id) if raw_job_id else None
            binding = (
                payload_job.host_image_binding
                if payload_job is not None and payload_job.session_id == raw_session_id
                else session.active_job.host_image_binding
                if session is not None and session.active_job is not None
                else None
            )
            sensitive_job_event = bool(
                session is not None
                and session.sensitive_media
                and payload_job is not None
                and payload_job.session_id == session.session_id
            )
            if sensitive_job_event:
                event_payload = payload.get("payload")
                event_message = (
                    str(event_payload.get("message") or "")
                    if isinstance(event_payload, dict)
                    else ""
                )
                canonical_job_cancelled = payload.get("type") == "warning_emitted" and (
                    event_message.startswith(f"job_cancelled {raw_job_id} reason=")
                )
                cancellation_accepted = bool(
                    payload_job.cancellation_event.is_set()
                    or payload_job.status in {"cancellation_requested", "cancelled"}
                )
                if cancellation_accepted and not canonical_job_cancelled:
                    return
                clean_payload = _redact_sensitive_media_projection(
                    session,
                    payload,
                    host_image_binding=binding,
                )
                if not isinstance(clean_payload, dict):
                    return
                self._buffer_event_locked(clean_payload)
                # Hold the same transition lock used by session.cancel through
                # the observable write. An event which wins this lock is fully
                # visible before Stop; once Stop wins, later job output is dropped.
                self._write(clean_payload)
                return
        clean_payload = (
            _redact_sensitive_media_projection(
                session,
                payload,
                host_image_binding=binding,
            )
            if session is not None and session.sensitive_media
            else redact_secrets(payload)
        )
        if not isinstance(clean_payload, dict):
            return
        with self._state_lock:
            self._buffer_event_locked(clean_payload)
        self._write(clean_payload)


APPROVAL_SCOPE_GRANTS_MAX = 32
SWARM_RECOVERY_GRANT_KIND = "forge_swarm_resume"
SWARM_RECOVERY_SCOPE_TYPE = "forge_swarm_resume_v1"


def _browser_public_url(raw_url: Any) -> str | None:
    value = str(raw_url or "").strip()
    if not value:
        return None
    try:
        split = urlsplit(value)
        scheme = split.scheme.lower()
        hostname = split.hostname
        if scheme not in {"http", "https"} or not hostname:
            return None
        host = f"[{hostname}]" if ":" in hostname else hostname
        if split.port is not None:
            host = f"{host}:{split.port}"
        public_url = urlunsplit((scheme, host, split.path or "/", "", ""))
    except (TypeError, ValueError):
        return None
    return str(redact_secrets(public_url))


def _browser_status_payload(status: BrowserSessionStatus) -> dict[str, Any]:
    network_scope = "public"
    if status.allow_local_destinations:
        network_scope = (
            "public_loopback"
            if bool(getattr(status, "local_destinations_loopback_only", False))
            else "local_network"
        )
    return {
        "browser_session_id": status.session_id,
        "product": status.product,
        "state": status.state,
        "created_at": status.created_at,
        "allow_local_destinations": status.allow_local_destinations,
        "network_scope": network_scope,
        "active_url": _browser_public_url(status.active_url),
        "artifact_count": status.artifact_count,
    }


def _browser_artifact_payload(artifact: BrowserArtifact) -> dict[str, Any]:
    browser_session_id = artifact.artifact_id.split(":", 2)[1]
    return {
        "browser_session_id": browser_session_id,
        "artifact_id": artifact.artifact_id,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
    }


def _browser_protocol_payload(ide_session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    clean = redact_secrets(payload)
    if not isinstance(clean, dict):
        raise ProtocolError("browser_error", "Browser result was invalid.")
    if "url" in payload:
        # Navigation results are model-visible and persisted by IDE clients.
        # Redaction alone cannot recognize every signed/OAuth query parameter,
        # so apply the same structural URL minimization used by status/list.
        clean["url"] = _browser_public_url(payload.get("url"))
    browser_session_id = str(clean.pop("session_id", "") or "")
    return {
        "session_id": ide_session_id,
        "browser_session_id": browser_session_id,
        **clean,
    }


def _optional_browser_timeout(params: dict[str, Any], *, request_id: RequestId) -> float | None:
    raw = params.get("timeout_seconds")
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ProtocolError(
            "invalid_field", "timeout_seconds must be a positive number.", request_id=request_id
        )
    try:
        timeout = float(raw)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(
            "invalid_field", "timeout_seconds must be a positive number.", request_id=request_id
        ) from exc
    if not 0.05 <= timeout <= 300 or timeout != timeout:
        raise ProtocolError(
            "invalid_field",
            "timeout_seconds must be between 0.05 and 300.",
            request_id=request_id,
        )
    return timeout


def _validated_approval_scope_grants(
    raw: Any,
    *,
    request_id: RequestId,
) -> list[ApprovalScopeRecord]:
    """Validate launch-time approval pre-grants.

    Grants reuse the exact allow-for-session scope shape the IDE already
    receives in approval prompts: ``{"kind": ..., "scope": {"type": ..., ...}}``.
    Recording them into approved_approval_scopes means matching dangerous
    actions auto-allow through the existing scope-matching semantics.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ProtocolError(
            "invalid_approval_scope_grant",
            "approval_scope_grants must be an array of {kind, scope} objects.",
            request_id=request_id,
        )
    if len(raw) > APPROVAL_SCOPE_GRANTS_MAX:
        raise ProtocolError(
            "invalid_approval_scope_grant",
            f"approval_scope_grants accepts at most {APPROVAL_SCOPE_GRANTS_MAX} entries.",
            request_id=request_id,
        )
    grants: list[ApprovalScopeRecord] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ProtocolError(
                "invalid_approval_scope_grant",
                f"approval_scope_grants[{index}] must be an object.",
                request_id=request_id,
            )
        kind = str(entry.get("kind") or "").strip()
        scope_raw = entry.get("scope")
        if not kind or not isinstance(scope_raw, dict) or not scope_raw:
            raise ProtocolError(
                "invalid_approval_scope_grant",
                f"approval_scope_grants[{index}] requires a non-empty kind and scope object.",
                request_id=request_id,
            )
        scope = _json_object(scope_raw)
        if not _scope_type(scope):
            raise ProtocolError(
                "invalid_approval_scope_grant",
                f"approval_scope_grants[{index}].scope requires a scope type.",
                request_id=request_id,
            )
        grants.append(ApprovalScopeRecord(kind=kind, scope=scope, key=_scope_key(scope)))
    return grants


def _swarm_resume_permission_scope(
    grants: list[ApprovalScopeRecord],
    *,
    session_id: str,
    plan_id: str,
    job_id: str,
    expected_revision: int | None,
    request_id: RequestId,
) -> tuple[dict[str, Any], list[ApprovalScopeRecord], dict[str, Any]]:
    """Separate one fresh job-scoped recovery grant from normal action authority."""

    recovery_grant: ApprovalScopeRecord | None = None
    action_grants: list[ApprovalScopeRecord] = []
    for grant in grants:
        scope_type = _scope_type(grant.scope)
        is_recovery_grant = (
            grant.kind == SWARM_RECOVERY_GRANT_KIND or scope_type == SWARM_RECOVERY_SCOPE_TYPE
        )
        if not is_recovery_grant:
            action_grants.append(grant)
            continue
        if recovery_grant is not None:
            raise ProtocolError(
                "invalid_approval_scope_grant",
                "Forge swarm recovery accepts exactly one fresh recovery grant.",
                request_id=request_id,
            )
        recovery_grant = grant

    if recovery_grant is None:
        raise ProtocolError(
            "swarm_fresh_permission_grant_required",
            "A fresh, job-scoped permission grant is required to resume this swarm job.",
            request_id=request_id,
        )
    scope = recovery_grant.scope
    if (
        recovery_grant.kind != SWARM_RECOVERY_GRANT_KIND
        or set(scope) != {"type", "session_id", "plan_id", "job_id", "revision"}
        or scope.get("type") != SWARM_RECOVERY_SCOPE_TYPE
        or scope.get("session_id") != session_id
        or scope.get("plan_id") != plan_id
        or scope.get("job_id") != job_id
        or isinstance(scope.get("revision"), bool)
        or not isinstance(scope.get("revision"), int)
        or (expected_revision is not None and scope.get("revision") != expected_revision)
    ):
        raise ProtocolError(
            "invalid_approval_scope_grant",
            "Forge swarm recovery requires one exact job-scoped recovery grant.",
            request_id=request_id,
        )
    fresh_permission_grant = {
        "schema_version": 1,
        "session_id": session_id,
        "job_id": job_id,
        "revision": int(scope["revision"]),
    }
    return _swarm_permission_scope(action_grants), action_grants, fresh_permission_grant


def _swarm_permission_scope(grants: list[ApprovalScopeRecord]) -> dict[str, Any]:
    """Return a stable, non-reversible fingerprint input for durable action-scope checks."""

    digests = {
        hashlib.sha256(f"{grant.kind}\0{grant.key}".encode()).hexdigest() for grant in grants
    }
    return {"schema_version": 1, "approval_scope_digests": sorted(digests)}


def _forge_swarm_usage(sessions_dir: Path) -> SwarmUsage:
    try:
        paths = sorted(sessions_dir.rglob("*.jsonl")) if sessions_dir.is_dir() else []
        records = aggregate_usage_from_session_logs(paths).records()
    except OSError:
        records = []
    return SwarmUsage(
        calls=len(records),
        input_tokens=sum(max(0, record.prompt_tokens) for record in records),
        output_tokens=sum(max(0, record.completion_tokens) for record in records),
        cached_input_tokens=sum(max(0, record.cached_prompt_tokens or 0) for record in records),
        total_tokens=sum(max(0, record.total_tokens) for record in records),
    )


def _forge_swarm_job_sessions_dir(sessions_dir: Path, job_id: str) -> Path:
    """Return a deterministic, traversal-safe session-log root for one durable job."""

    job_key = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    return sessions_dir / "ide_swarm_jobs" / job_key


def _swarm_usage_delta(total: SwarmUsage, recorded: SwarmUsage) -> SwarmUsage:
    return SwarmUsage(
        calls=max(0, total.calls - recorded.calls),
        input_tokens=max(0, total.input_tokens - recorded.input_tokens),
        output_tokens=max(0, total.output_tokens - recorded.output_tokens),
        cached_input_tokens=max(0, total.cached_input_tokens - recorded.cached_input_tokens),
        total_tokens=max(0, total.total_tokens - recorded.total_tokens),
    )


def _approval_kind(request: ApprovalRequest) -> str:
    return str(request.kind or "").strip() or "generic"


def _approval_scope_for_request(
    kind: str,
    request: ApprovalRequest,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    if kind == "computer_use":
        scope = _raw_approval_scope(request)
        if scope and scope.get("type") == "computer_use_turn_action":
            return scope, _scope_key(scope), None
        return None, None, "Computer Use requires an exact action scope for this turn."

    if _is_command_scoped_approval_kind(kind):
        command = _approval_command_scope_text(kind, request)
        if not command:
            return None, None, "Allow for session requires an exact command scope."
        scope = exact_command_scope(command, kind=kind)
        return scope, _scope_key(scope), None

    if _is_file_approval_kind(kind):
        files = _normalized_files(request.files)
        if not files:
            return None, None, "Allow for session requires an exact file path set."
        scope = exact_file_set_scope(files, operation=kind)
        scope["kind"] = kind
        return scope, _scope_key(scope), None

    if kind == "verify_run":
        raw_scope = _raw_approval_scope(request)
        if _scope_type(raw_scope) == SCOPE_EXACT_VERIFY_COMMAND_SET:
            scope = dict(raw_scope or {})
            scope["type"] = SCOPE_EXACT_VERIFY_COMMAND_SET
            scope["kind"] = kind
            return scope, _scope_key(scope), None
        commands = _verification_commands_from_preview(request.preview)
        if not commands:
            return None, None, "Allow for session requires an exact verification command set."
        scope = exact_verify_command_set_scope(commands)
        scope["kind"] = kind
        return scope, _scope_key(scope), None

    return None, None, "Allow for session is unavailable for this approval kind."


def _approval_command_scope_text(kind: str, request: ApprovalRequest) -> str:
    if kind in SHELL_APPROVAL_KINDS:
        return str(request.command or request.preview or "").strip()
    if kind.startswith("mcp_tool_run:"):
        # "Allow for this session" covers one MCP tool (its alias), whatever the arguments.
        # Destructive MCP tools never get a session scope (see _request_approval).
        return str(request.command or "").strip()
    return str(request.preview or request.command or "").strip()


def _is_command_scoped_approval_kind(kind: str) -> bool:
    return (
        kind in SHELL_APPROVAL_KINDS
        or kind.startswith("custom_tool_run:")
        or kind.startswith("mcp_")
        or kind.startswith("mcp_tool_run:")
    )


def _is_file_approval_kind(kind: str) -> bool:
    return kind in FILE_APPROVAL_KINDS


def _raw_approval_scope(request: ApprovalRequest) -> dict[str, Any] | None:
    raw_scope = request.allow_for_session_scope
    if isinstance(raw_scope, dict):
        return dict(raw_scope)
    metadata = request.metadata
    if isinstance(metadata, dict):
        candidate = metadata.get("allow_for_session_scope")
        if isinstance(candidate, dict):
            return dict(candidate)
    return None


def _scope_type(scope: dict[str, Any] | None) -> str:
    if not isinstance(scope, dict):
        return ""
    return str(scope.get("type") or scope.get("scope_type") or "").strip()


def _verification_commands_from_preview(preview: str) -> list[str]:
    commands: list[str] = []
    for line in str(preview or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("$ "):
            command = stripped[2:].strip()
            if command:
                commands.append(command)
    return commands


def _approved_scope_matches(
    approved_scopes: list[ApprovalScopeRecord],
    kind: str,
    scope: dict[str, Any] | None,
) -> bool:
    if scope is None:
        return False
    scope_type = _scope_type(scope)
    for approved in approved_scopes:
        approved_type = _scope_type(approved.scope)
        if approved_type != scope_type or approved.kind != kind:
            continue
        if scope_type == "computer_use_turn_action":
            if approved.scope == scope:
                return True
        elif scope_type == SCOPE_EXACT_COMMAND_HASH:
            if approved.scope.get("command_hash") == scope.get("command_hash"):
                return True
        elif scope_type == SCOPE_EXACT_FILE_SET:
            requested_files = set(_string_list(scope.get("files")))
            approved_files = set(_string_list(approved.scope.get("files")))
            if requested_files and requested_files.issubset(approved_files):
                return True
        elif scope_type == SCOPE_EXACT_VERIFY_COMMAND_SET:
            if approved.scope.get("commands_hash") == scope.get(
                "commands_hash"
            ) and approved.scope.get("command_count") == scope.get("command_count"):
                return True
    return False


def _approval_prompt_event(pending: PendingApprovalRecord) -> ProtocolPayloadEvent:
    metadata = dict(pending.metadata)
    metadata.setdefault("approval_kind", pending.kind)
    metadata.setdefault("kind", pending.kind)
    binding_payload = _approval_binding_payload(pending)
    return ProtocolPayloadEvent(
        "approval.required" if pending.kind == "computer_use" else "prompt_for_input",
        {
            "prompt_id": pending.approval_id,
            "prompt_text": pending.preview
            or pending.reason
            or f"Approval required: {pending.kind}",
            "kind": "approval",
            "approval_id": pending.approval_id,
            "approval_kind": pending.kind,
            "reason": pending.reason,
            "preview": pending.preview,
            "files": list(pending.files),
            "command": pending.command,
            "metadata": metadata,
            "expires_at": pending.expires_at,
            "scope": pending.scope,
            "allow_for_session_supported": pending.allow_for_session_supported,
            "allow_for_session_scope": pending.scope
            if pending.allow_for_session_supported
            else None,
            "allow_for_session_warning": pending.allow_for_session_warning,
            **binding_payload,
        },
    )


def _approval_result_event(
    pending: PendingApprovalRecord,
    decision: ApprovalDecision,
    status: str,
) -> ProtocolPayloadEvent:
    if pending.kind == "computer_use":
        return ProtocolPayloadEvent(
            "prompt_for_input",
            {
                "kind": "approval_result",
                "approval_id": pending.approval_id,
                "approval_kind": pending.kind,
                "status": status,
                "allow": decision.allow,
                "allow_for_session": decision.allow_for_session,
            },
        )
    metadata = {
        "status": status,
        "allow": bool(decision.allow),
        "allow_for_session": bool(decision.allow_for_session),
        "allow_for_session_supported": pending.allow_for_session_supported,
    }
    binding_payload = _approval_binding_payload(pending, include_action=False)
    return ProtocolPayloadEvent(
        "prompt_for_input",
        {
            "kind": "approval_result",
            "approval_id": pending.approval_id,
            "status": status,
            "allow": bool(decision.allow),
            "allow_for_session": bool(decision.allow_for_session),
            "allow_for_session_supported": pending.allow_for_session_supported,
            "metadata": metadata,
            **binding_payload,
        },
    )


def _approval_response(
    pending: PendingApprovalRecord,
    status: str,
    *,
    decision: ApprovalDecision | None = None,
) -> dict[str, Any]:
    decision = decision or pending.decision or ApprovalDecision(allow=False)
    return {
        "session_id": pending.session_id,
        "approval_id": pending.approval_id,
        "status": status,
        "allow": bool(decision.allow),
        "allow_for_session": bool(decision.allow_for_session),
        "allow_for_session_supported": pending.allow_for_session_supported,
        "allow_for_session_scope": pending.scope if pending.allow_for_session_supported else None,
        "allow_for_session_warning": pending.allow_for_session_warning,
        **_approval_binding_payload(pending, include_action=False),
    }


def _approval_binding_payload(
    pending: PendingApprovalRecord,
    *,
    include_action: bool = True,
) -> dict[str, Any]:
    if pending.binding is None:
        return {}
    payload = pending.binding.renderer_payload()
    if not include_action:
        payload.pop("action", None)
    return _json_clone(redact_secrets(payload))


def _validate_optional_approval_bindings(
    params: dict[str, Any],
    pending: PendingApprovalRecord,
    *,
    request_id: RequestId,
) -> None:
    binding = pending.binding
    if binding is None:
        return
    expected: dict[str, str | None] = {
        "action_fingerprint": binding.action_fingerprint,
        "project_fingerprint": binding.project_fingerprint,
        "runtime_fingerprint": binding.runtime_fingerprint,
        "policy_fingerprint": binding.policy_fingerprint,
        "operation_id": binding.operation_id,
        "job_id": binding.job_id,
        "tool_call_id": binding.tool_call_id,
        "expires_at": pending.expires_at,
    }
    for field_name, expected_value in expected.items():
        if field_name not in params:
            if pending.kind == "computer_use":
                raise ProtocolError(
                    "approval_binding_mismatch",
                    "Computer Use decisions require every approval binding.",
                    request_id=request_id,
                )
            continue
        supplied = params[field_name]
        if supplied is not None and not isinstance(supplied, str):
            raise ProtocolError(
                "invalid_request",
                f"{field_name} must be a string or null.",
                request_id=request_id,
            )
        normalized = supplied.strip() if isinstance(supplied, str) else None
        if normalized != expected_value:
            raise ProtocolError(
                "approval_binding_mismatch",
                f"{field_name} does not match the pending approval.",
                request_id=request_id,
            )


def _scope_key(scope: dict[str, Any]) -> str:
    return json.dumps(scope, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _normalized_files(files: Any) -> list[str]:
    if not isinstance(files, list | tuple):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in files:
        text = str(item).strip().replace("\\", "/")
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return sorted(normalized)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item) for item in value if str(item)]


def _json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return json.loads(json.dumps(value, ensure_ascii=True, default=str))


def _computer_use_approval_expiry(value: Any) -> datetime | None:
    """Exact Date.toISOString wire form; only the authenticated host supplies it."""
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z", value
    ):
        return None
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Control actions allow up to two minutes for human review. A shorter host
    # expiry (including file/upload challenges) remains authoritative throughout.
    if expiry > datetime.now(UTC) + timedelta(seconds=MAX_COMPUTER_USE_APPROVAL_SECONDS):
        return None
    return expiry


def _approval_deadline_expired(
    pending: PendingApprovalRecord, monotonic_now: float | None = None
) -> bool:
    return (time.monotonic() if monotonic_now is None else monotonic_now) >= pending.deadline or (
        pending.host_expires_at is not None and datetime.now(UTC) >= pending.host_expires_at
    )


def _expires_at(timeout_seconds: float) -> str:
    return (
        (datetime.now(UTC) + timedelta(seconds=max(0.0, timeout_seconds)))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _reject_inline_secrets(params: dict[str, Any], *, request_id: RequestId) -> None:
    if not _contains_secret_key(params):
        return
    raise ProtocolError(
        "inline_secret_rejected",
        "Secrets must be provided through configured Alysis Code credential sources, not IDE protocol params.",
        request_id=request_id,
    )


def _reject_computer_response_inline_secrets(
    params: dict[str, Any], *, pending: PendingComputerUseRequest, ok: bool, request_id: RequestId
) -> None:
    """Permit one bound host preflight capability, never general credential input."""
    guarded_params = params
    result = params.get("result")
    preflight = result.get("approval_preflight") if isinstance(result, dict) else None
    if (
        ok
        and pending.tool_name in {"computer_action", "computer_download", "computer_upload"}
        and pending.approval_preflight_expected
        and isinstance(preflight, dict)
        and type(preflight.get("version")) is int
        and preflight.get("version") == 1
        and type(preflight.get("required")) is bool
        and isinstance(preflight.get("token"), str)
        and re.fullmatch(r"[a-f0-9]{64}", preflight["token"])
        and isinstance(preflight.get("scope_digest"), str)
        and re.fullmatch(r"sha256:[a-f0-9]{64}", preflight["scope_digest"])
    ):
        # Shallow copies remove only this leaf from the inspection view. Keep
        # the original host response/token exact for preparation and execution;
        # malicious neighboring fields remain subject to the recursive guard.
        guarded_params = {
            **params,
            "result": {
                **result,
                "approval_preflight": {
                    key: value for key, value in preflight.items() if key != "token"
                },
            },
        }
    _reject_inline_secrets(guarded_params, request_id=request_id)


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _is_secret_key(key) or _contains_secret_key(child) for key, child in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_contains_secret_key(child) for child in value)
    return False


def _is_secret_key(key: object) -> bool:
    lowered = str(key).lower()
    if lowered in {"api_key_env"}:
        return False
    return any(
        marker in lowered
        for marker in ("api_key", "token", "secret", "password", "credential", "authorization")
    )


def _mcp_lifecycle_protocol_error(
    exc: ConfigError | RuntimeError,
    *,
    request_id: RequestId,
) -> ProtocolError:
    if isinstance(exc, McpError):
        # MCP transport/client errors can contain a server-controlled stderr
        # tail or remote response body. Pattern redaction is defense in depth,
        # not proof that arbitrary credential material was recognized, so the
        # IDE protocol never echoes those diagnostics.
        return ProtocolError(
            "mcp_lifecycle_error",
            "The MCP server lifecycle operation failed. Server diagnostics were withheld; "
            "review the server configuration in a trusted local environment, then retry or "
            "recreate the IDE session.",
            request_id=request_id,
        )
    return ProtocolError(
        "mcp_lifecycle_error",
        str(redact_secrets(str(exc))),
        request_id=request_id,
    )


def _mode_param(params: dict[str, Any], *, request_id: RequestId) -> str:
    mode = str(params.get("mode") or "review").strip().lower()
    if mode not in SUPPORTED_MODES:
        raise ProtocolError(
            "invalid_mode",
            f"Unsupported mode: {mode or '(missing)'}.",
            request_id=request_id,
        )
    return mode


def _code_review_request(params: dict[str, Any], *, request_id: RequestId) -> ReviewRequest:
    scope = str(params.get("scope") or "working_tree").strip().lower()
    try:
        if scope == "working_tree":
            review = ReviewRequest.working_tree()
        elif scope == "branch":
            review = ReviewRequest.branch(
                base=_required_str(params, "base", request_id=request_id),
                head=_optional_str(params, "head", request_id=request_id) or "HEAD",
            )
        elif scope == "commit":
            review = ReviewRequest.commit(_required_str(params, "revision", request_id=request_id))
        elif scope == "range":
            review = ReviewRequest.revision_range(
                base=_required_str(params, "base", request_id=request_id),
                head=_required_str(params, "head", request_id=request_id),
            )
        else:
            raise InvalidReviewRequest(f"unsupported review scope: {scope or '(missing)'}")
        review.validate()
        return review
    except InvalidReviewRequest as exc:
        raise ProtocolError(
            "invalid_review_request",
            str(redact_secrets(exc)),
            request_id=request_id,
        ) from exc


def _apply_session_sandbox_profile(
    cfg: Any, params: dict[str, Any], *, request_id: RequestId
) -> None:
    if "sandbox_profile" in params:
        profile = _optional_str(params, "sandbox_profile", request_id=request_id) or "default"
        if profile not in {"default", "strict", "warn", "off"}:
            raise ProtocolError(
                "invalid_field", "Unsupported sandbox_profile.", request_id=request_id
            )
        if profile != "default":
            apply_sandbox_mode_to_config(cfg, profile)


def _apply_config_overrides(cfg: Any, params: dict[str, Any], *, request_id: RequestId) -> None:
    model = str(params.get("model") or "").strip()
    base_url = str(params.get("base_url") or "").strip()
    if model:
        cfg.model = model
    if base_url:
        normalized_base_url = _validate_ide_base_url(base_url, request_id=request_id)
        cfg.base_url = normalized_base_url
        _activate_transient_native_profile(
            cfg,
            model=str(getattr(cfg, "model", "") or "").strip(),
            base_url=normalized_base_url,
        )
    if "temperature" in params and params.get("temperature") is not None:
        try:
            temperature = float(params["temperature"])
        except (TypeError, ValueError) as exc:
            raise ProtocolError(
                "invalid_field", "temperature must be a number.", request_id=request_id
            ) from exc
        if not 0 <= temperature <= 2:
            raise ProtocolError(
                "invalid_field",
                "temperature must be between 0 and 2.",
                request_id=request_id,
            )
        _apply_legacy_temperature_override(cfg, temperature)
    if "stream" in params:
        cfg.stream = _optional_bool(
            params, "stream", default=bool(getattr(cfg, "stream", False)), request_id=request_id
        )


def _computer_use_destination_param(
    params: dict[str, Any],
    *,
    request_id: RequestId,
) -> ComputerUseDestinationBinding | None:
    raw = params.get("computer_use_destination")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProtocolError(
            "invalid_computer_use_destination",
            "computer_use_destination must be an object.",
            request_id=request_id,
        )
    expected_fields = {
        "version",
        "model",
        "profile",
        "auth_source",
        "account_id",
        "endpoint_digest",
        "route_fingerprint",
    }
    if set(raw) != expected_fields:
        raise ProtocolError(
            "invalid_computer_use_destination",
            "computer_use_destination has an invalid schema.",
            request_id=request_id,
        )
    if raw.get("version") != COMPUTER_USE_DESTINATION_VERSION:
        raise ProtocolError(
            "invalid_computer_use_destination",
            "computer_use_destination version is unsupported.",
            request_id=request_id,
        )
    values: dict[str, str] = {}
    for field_name in ("model", "profile", "auth_source", "account_id"):
        value = raw.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ProtocolError(
                "invalid_computer_use_destination",
                f"computer_use_destination.{field_name} must be a non-empty string.",
                request_id=request_id,
            )
        values[field_name] = _identity_text(value)
    endpoint_digest = raw.get("endpoint_digest")
    route_fingerprint = raw.get("route_fingerprint")
    if (
        not isinstance(endpoint_digest, str)
        or _SHA256_WITH_PREFIX_PATTERN.fullmatch(endpoint_digest) is None
        or not isinstance(route_fingerprint, str)
        or _SHA256_WITH_PREFIX_PATTERN.fullmatch(route_fingerprint) is None
    ):
        raise ProtocolError(
            "invalid_computer_use_destination",
            "computer_use_destination digests are invalid.",
            request_id=request_id,
        )
    binding = _computer_use_destination(
        model=values["model"],
        profile=values["profile"],
        auth_source=values["auth_source"],
        account_id=values["account_id"],
        endpoint_digest=endpoint_digest,
    )
    if not hmac.compare_digest(binding.route_fingerprint, route_fingerprint):
        raise ProtocolError(
            "invalid_computer_use_destination",
            "computer_use_destination fingerprint is invalid.",
            request_id=request_id,
        )
    return binding


def _browser_start_host_param(
    params: dict[str, Any], *, request_id: RequestId
) -> dict[str, Any] | None:
    if "browser_start_host" not in params:
        return None
    raw = params["browser_start_host"]
    valid = (
        isinstance(raw, dict)
        and {"version", "operation_id", "host_nonce"}
        <= set(raw)
        <= {"version", "operation_id", "host_nonce", "discovery"}
        and type(raw.get("version")) is int
        and raw["version"] == 1
        and isinstance(raw.get("operation_id"), str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", raw["operation_id"]) is not None
        and isinstance(raw.get("host_nonce"), str)
    )
    try:
        valid = valid and str(uuid.UUID(raw["host_nonce"])) == raw["host_nonce"]
    except (ValueError, AttributeError, TypeError):
        valid = False
    if not valid:
        raise ProtocolError(
            "invalid_browser_start_host",
            "Browser start host binding is invalid.",
            request_id=request_id,
        )
    if "discovery" in raw:
        d = raw["discovery"]
        try:
            valid = (
                isinstance(d, dict)
                and set(d) == {"version", "targets"}
                and type(d["version"]) is int
                and d["version"] == 1
                and isinstance(d["targets"], list)
                and len(d["targets"]) <= 128
                and all(
                    isinstance(t, dict)
                    and {"target_id", "name", "route"}
                    <= set(t)
                    <= {"target_id", "name", "route", "availability"}
                    and (
                        "availability" not in t
                        or t["availability"] == "installed"
                        and t["route"] == "native"
                    )
                    and str(uuid.UUID(t["target_id"])) == t["target_id"]
                    and isinstance(t["name"], str)
                    and 1 <= len(t["name"]) <= 128
                    and t["route"] in {"native", "chrome"}
                    for t in d["targets"]
                )
                and len({t["target_id"] for t in d["targets"]}) == len(d["targets"])
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            valid = False
        if not valid:
            raise ProtocolError(
                "invalid_browser_start_host",
                "Invalid discovery capabilities.",
                request_id=request_id,
            )
    # Nested inventory is a host capability; caller mutation cannot change it after validation.
    import copy

    return copy.deepcopy(raw)


def _computer_use_host_param(
    params: dict[str, Any],
    *,
    request_id: RequestId,
) -> ComputerUseHostBinding | None:
    raw = params.get("computer_use_host")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProtocolError(
            "invalid_computer_use_host",
            "computer_use_host must be an object.",
            request_id=request_id,
        )
    required_fields = {
        "version",
        "operation_id",
        "broker_fingerprint",
        "authority_epoch",
        "scope_digest",
        "tools",
    }
    actual_fields = set(raw)
    if (
        not required_fields <= actual_fields
        or not actual_fields
        <= {
            *required_fields,
            "response_mode",
            "task_budget",
            "upload_objective",
            "observation_version",
        }
        or raw.get("version") != 1
    ):
        raise ProtocolError(
            "invalid_computer_use_host",
            "computer_use_host has an invalid or unsupported schema.",
            request_id=request_id,
        )
    operation_id = raw.get("operation_id")
    if (
        not isinstance(operation_id, str)
        or not operation_id.strip()
        or operation_id != operation_id.strip()
        or len(operation_id) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in operation_id)
    ):
        raise ProtocolError(
            "invalid_computer_use_host",
            "computer_use_host.operation_id is invalid.",
            request_id=request_id,
        )
    broker_fingerprint = raw.get("broker_fingerprint")
    scope_digest = raw.get("scope_digest")
    if (
        not isinstance(broker_fingerprint, str)
        or _SHA256_WITH_PREFIX_PATTERN.fullmatch(broker_fingerprint) is None
        or not isinstance(scope_digest, str)
        or _SHA256_WITH_PREFIX_PATTERN.fullmatch(scope_digest) is None
    ):
        raise ProtocolError(
            "invalid_computer_use_host",
            "computer_use_host fingerprints are invalid.",
            request_id=request_id,
        )
    authority_epoch = raw.get("authority_epoch")
    if (
        not isinstance(authority_epoch, int)
        or isinstance(authority_epoch, bool)
        or authority_epoch < 1
    ):
        raise ProtocolError(
            "invalid_computer_use_host",
            "computer_use_host.authority_epoch must be a positive integer.",
            request_id=request_id,
        )
    tools = raw.get("tools")
    admitted_tool_sets = tuple(
        COMPUTER_USE_TOOL_NAMES + verification + files + uploads
        for verification in ((), COMPUTER_USE_VERIFICATION_TOOL_NAMES)
        for files in ((), COMPUTER_USE_FILE_TOOL_NAMES)
        for uploads in ((), COMPUTER_USE_UPLOAD_TOOL_NAMES)
    )
    if not isinstance(tools, list) or tuple(tools) not in admitted_tool_sets:
        raise ProtocolError(
            "invalid_computer_use_host",
            "computer_use_host must advertise exactly the fixed Computer Use tool set.",
            request_id=request_id,
        )
    if "observation_version" in raw and (
        type(raw["observation_version"]) is not int or raw["observation_version"] != 1
    ):
        raise ProtocolError(
            "invalid_computer_use_host", "Unsupported observation version.", request_id=request_id
        )
    response_mode = raw.get("response_mode", "status_only")
    if response_mode not in COMPUTER_USE_RESPONSE_MODES:
        raise ProtocolError(
            "invalid_computer_use_host",
            "computer_use_host.response_mode is unsupported.",
            request_id=request_id,
        )
    try:
        # Present null is not an absent feature: configuration must be exact.
        if "upload_objective" in raw and raw["upload_objective"] is None:
            raise ValueError("Invalid upload objective binding")
        objective = capture_upload_objective(raw.get("upload_objective"))
        if objective is not None and not (
            "computer_upload" in tools and "computer_verify" in tools
        ):
            raise ValueError("Missing objective evidence tools")
    except ValueError as exc:
        raise ProtocolError(
            "invalid_computer_use_host",
            "computer_use_host.upload_objective is invalid.",
            request_id=request_id,
        ) from exc
    return ComputerUseHostBinding(
        operation_id=operation_id,
        broker_fingerprint=broker_fingerprint,
        authority_epoch=authority_epoch,
        scope_digest=scope_digest,
        response_mode=response_mode,
        observation_version=raw.get("observation_version"),
        verification_available="computer_verify" in tools,
        files_available="computer_download" in tools,
        uploads_available="computer_upload" in tools,
        task_budget=parse_task_budget_binding(raw["task_budget"]) if "task_budget" in raw else None,
        upload_objective=objective,
    )


def _apply_computer_use_route_overrides(
    cfg: Any,
    params: dict[str, Any],
    destination: ComputerUseDestinationBinding,
    *,
    request_id: RequestId,
) -> None:
    """Bind a cloned session to its already-selected route; never create a transient route."""

    model = _required_str(params, "model", request_id=request_id)
    raw_base_url = _required_str(params, "base_url", request_id=request_id)
    normalized_base_url = _validate_ide_base_url(raw_base_url, request_id=request_id)
    if (
        model != destination.model
        or _computer_use_endpoint_digest(normalized_base_url) != destination.endpoint_digest
    ):
        raise ProtocolError(
            "computer_use_destination_mismatch",
            "The requested model endpoint does not match the approved Computer Use destination.",
            request_id=request_id,
        )
    try:
        profile = get_active_profile(cfg)
        actual_endpoint = resolve_effective_base_url(cfg=cfg, profile=profile)
    except ConfigError as exc:
        raise ProtocolError("config_error", str(exc), request_id=request_id) from exc
    if (
        profile.name != destination.profile
        or _computer_use_endpoint_digest(actual_endpoint) != destination.endpoint_digest
    ):
        raise ProtocolError(
            "computer_use_destination_mismatch",
            "The loaded provider route does not match the approved Computer Use destination.",
            request_id=request_id,
        )
    # Do not feed base_url into the ordinary IDE override path: it would manufacture
    # an unauthenticated transient native profile and could silently replace a selected
    # subscription/client route. The active cloned profile is the only allowed route.
    cfg.model = model


def _assert_task_budget_client_supported(agent: Any) -> None:
    from ..agent.session import AgentSession
    from ..llm.anthropic_messages import AnthropicMessagesClient
    from ..llm.gemini_generate_content import GeminiGenerateContentClient
    from ..llm.gemini_interactions import GeminiInteractionsClient
    from ..llm.openai_compat import OpenAICompatClient
    from ..llm.openai_responses import OpenAIResponsesClient
    from ..model_registry import ModelRegistry
    from ..provider_auth.openai_codex import OpenAICodexSubscriptionAuth

    client = getattr(agent, "client", None)
    budget_need(
        type(agent) is AgentSession
        and type(client)
        in {
            OpenAIResponsesClient,
            OpenAICompatClient,
            AnthropicMessagesClient,
            GeminiGenerateContentClient,
            GeminiInteractionsClient,
        }
    )
    budget_need(
        type(agent.model_registry) is ModelRegistry
        and not agent.model_registry._provider_auth_metadata_allowed
    )
    budget_need(
        agent.sensitive_media
        and agent.conversation_compactor is None
        and not agent.subagents_enabled
    )
    budget_need(getattr(client, "_transport", None) is None)
    auth = getattr(client, "provider_auth", None)
    budget_need(auth is None or type(auth) is OpenAICodexSubscriptionAuth)
    budget_need(not getattr(client, "explicit_cached_content_enabled", False))


def _actual_computer_use_destination(
    agent_session: Any,
    *,
    request_id: RequestId,
) -> ComputerUseDestinationBinding:
    cfg = getattr(agent_session, "cfg", None)
    client = getattr(agent_session, "client", None)
    registry = getattr(agent_session, "model_registry", None)
    if cfg is None or client is None or registry is None:
        raise ProtocolError(
            "computer_use_route_unavailable",
            "The CLI cannot prove its live model route for a screen image.",
            request_id=request_id,
        )
    model = _identity_text(getattr(client, "model", ""))
    base_url = _identity_text(getattr(client, "base_url", ""))
    auth_source = _identity_text(getattr(agent_session, "api_key_source", ""))
    if not model or not base_url or not auth_source:
        raise ProtocolError(
            "computer_use_route_unavailable",
            "The CLI cannot prove its live model route for a screen image.",
            request_id=request_id,
        )
    try:
        profile = get_active_profile(cfg)
        # ModelRegistry is route-scoped by the cloned session config. Query it here
        # so vision metadata and the destination proof share the same registry/client.
        registry.get(model)
    except Exception as exc:  # noqa: BLE001 - fail closed at the host-image boundary.
        raise ProtocolError(
            "computer_use_route_unavailable",
            "The CLI cannot prove its live model route for a screen image.",
            request_id=request_id,
        ) from exc
    route_identity = getattr(client, "route_identity", None)
    account_id = _identity_text(getattr(route_identity, "credential_scope", ""))
    if not account_id:
        account_id = credential_scope_fingerprint(getattr(agent_session, "api_key", ""))
    if not account_id:
        raise ProtocolError(
            "computer_use_route_unavailable",
            "The CLI cannot prove its live account route for a screen image.",
            request_id=request_id,
        )
    return _computer_use_destination(
        model=model,
        profile=_identity_text(profile.name),
        auth_source=auth_source,
        account_id=account_id,
        endpoint_digest=_computer_use_endpoint_digest(base_url),
    )


def _computer_use_destination(
    *,
    model: str,
    profile: str,
    auth_source: str,
    account_id: str,
    endpoint_digest: str,
) -> ComputerUseDestinationBinding:
    canonical = json.dumps(
        {
            "account_id": account_id,
            "auth_source": auth_source,
            "endpoint_digest": endpoint_digest,
            "model": model,
            "profile": profile,
            "version": COMPUTER_USE_DESTINATION_VERSION,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    fingerprint = hashlib.sha256(f"computer-use-route-v1\0{canonical}".encode()).hexdigest()
    return ComputerUseDestinationBinding(
        model=model,
        profile=profile,
        auth_source=auth_source,
        account_id=account_id,
        endpoint_digest=endpoint_digest,
        route_fingerprint=f"sha256:{fingerprint}",
    )


def _computer_use_endpoint_digest(value: str) -> str:
    normalized = _normalize_computer_use_endpoint(value)
    digest = hashlib.sha256(f"computer-use-endpoint-v1\0{normalized}".encode()).hexdigest()
    return f"sha256:{digest}"


def _normalize_computer_use_endpoint(value: Any) -> str:
    raw = _identity_text(value)
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw
    if not parsed.scheme or not parsed.netloc:
        return raw
    try:
        hostname = str(parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return raw
    if not hostname:
        return raw
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    userinfo = parsed.netloc.rsplit("@", 1)[0] if "@" in parsed.netloc else ""
    authority = f"{userinfo}@" if userinfo else ""
    authority += hostname
    if port is not None and not (
        (parsed.scheme.lower() == "https" and port == 443)
        or (parsed.scheme.lower() == "http" and port == 80)
    ):
        authority += f":{port}"
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{authority}{path}" + (
        f"?{parsed.query}" if parsed.query else ""
    )


def _identity_text(value: Any) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).strip()


def _same_computer_use_destination(
    expected: ComputerUseDestinationBinding,
    actual: ComputerUseDestinationBinding | None,
) -> bool:
    return (
        actual is not None
        and hmac.compare_digest(
            expected.route_fingerprint,
            actual.route_fingerprint,
        )
        and (
            expected.model == actual.model
            and expected.profile == actual.profile
            and expected.auth_source == actual.auth_source
            and expected.account_id == actual.account_id
            and expected.endpoint_digest == actual.endpoint_digest
        )
    )


def _assert_live_computer_use_destination(
    session: BridgeSession,
    *,
    request_id: RequestId,
) -> None:
    expected = session.computer_use_destination
    if expected is None:
        raise ProtocolError(
            "computer_use_destination_required",
            "The sensitive host-context session has no destination binding.",
            request_id=request_id,
        )
    actual = _actual_computer_use_destination(
        session.agent_session,
        request_id=request_id,
    )
    if not _same_computer_use_destination(expected, actual):
        raise ProtocolError(
            "computer_use_destination_mismatch",
            "The live CLI client no longer matches the approved Computer Use destination.",
            request_id=request_id,
        )


def _host_image_binding_param(
    params: dict[str, Any],
    *,
    request_id: RequestId,
) -> HostImageBinding | None:
    if "host_image" not in params:
        return None
    try:
        return parse_host_image_binding_descriptor(
            params.get("host_image"),
            max_bytes=MAX_IDE_IMAGE_BYTES,
        )
    except HostImageValidationError as exc:
        raise ProtocolError(exc.code, exc.message, request_id=request_id) from exc


def _activate_bridge_host_screen_redactor(session: BridgeSession) -> None:
    """Install a redactor before the bridge touches a host-media descriptor.

    AgentSession replaces this redactor with its richer turn redactor once it
    builds the provider request. Starting one here covers the earlier
    descriptor/path-validation and thread-start error boundaries as well.
    """

    activate = getattr(session.agent_session, "activate_host_screen_redactor", None)
    if not callable(activate):
        return
    with suppress(Exception):
        activate(HostScreenRedactor())


def _extend_bridge_host_screen_redactor(
    session: BridgeSession,
    paths: list[str] | list[Path],
) -> None:
    redactor = getattr(session.agent_session, "host_screen_redactor", None)
    add_path = getattr(redactor, "add_path", None)
    if not callable(add_path):
        return
    for path in paths:
        with suppress(Exception):
            add_path(path)


def _clear_bridge_host_screen_redactor(session: BridgeSession) -> None:
    clear = getattr(session.agent_session, "clear_host_screen_redactor", None)
    if callable(clear):
        with suppress(Exception):
            clear()


def _redact_bridge_error(
    session: BridgeSession | None,
    value: Any,
    *,
    host_image_binding: HostImageBinding | None = None,
    image_paths: list[str] | None = None,
) -> str:
    """Redact a bridge failure before it can reach a job, event, or response.

    The session redactor knows data URLs that were materialized while building a
    request. A small bridge-local redactor supplements it after AgentSession's
    turn-finally has cleared that state, and the blob fallback handles provider
    error bodies that echo raw base64 without a ``data:image`` prefix.
    """

    redacted = _redact_sensitive_media_projection(
        session,
        str(value),
        host_image_binding=host_image_binding,
        image_paths=image_paths,
    )
    # Protected turns replace and scrub their original exception before reaching
    # this layer. Only this revalidated closed metadata can survive that boundary.
    from ..computer_use_diagnostics import protected_provider_diagnostic_marker

    marker = protected_provider_diagnostic_marker(value)
    return f"{redacted} {marker}" if marker else str(redacted)


def _redact_sensitive_media_projection(
    session: BridgeSession | None,
    value: Any,
    *,
    host_image_binding: HostImageBinding | None = None,
    image_paths: list[str] | None = None,
) -> Any:
    """Apply both turn-local and bridge-local host-media redaction recursively."""

    redacted: Any = value
    redact_host_screen_value = (
        getattr(session.agent_session, "redact_host_screen_value", None)
        if session is not None
        else None
    )
    if callable(redact_host_screen_value):
        with suppress(Exception):
            redacted = redact_host_screen_value(redacted)
    fallback = HostScreenRedactor()
    if host_image_binding is not None:
        fallback.add_path(host_image_binding.root)
    for path in image_paths or []:
        fallback.add_path(path)
    redacted = fallback.redact_value(redacted)
    redacted = redact_secrets(redacted)
    return _redact_sensitive_media_base64_blobs(redacted)


def _redact_sensitive_media_base64_blobs(value: Any) -> Any:
    if isinstance(value, str):
        return _SENSITIVE_MEDIA_BASE64_BLOB_PATTERN.sub(HOST_SCREEN_REDACTION_MARKER, value)
    if isinstance(value, list):
        return [_redact_sensitive_media_base64_blobs(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive_media_base64_blobs(item) for item in value)
    if isinstance(value, dict):
        return {
            _redact_sensitive_media_base64_blobs(key): _redact_sensitive_media_base64_blobs(item)
            for key, item in value.items()
        }
    return value


def _validate_ide_base_url(base_url: str, *, request_id: RequestId) -> str:
    try:
        normalized = validate_base_url(base_url)
        parsed = urlparse(normalized)
    except (ConfigError, ValueError) as exc:
        raise ProtocolError(
            "invalid_base_url",
            "base_url must be an absolute HTTP(S) URL without userinfo, query, or fragment.",
            request_id=request_id,
        ) from exc
    if parsed.username is not None or parsed.password is not None:
        raise ProtocolError(
            "invalid_base_url",
            "base_url must be an absolute HTTP(S) URL without userinfo, query, or fragment.",
            request_id=request_id,
        )
    return normalized


def _activate_transient_native_profile(cfg: Any, *, model: str, base_url: str) -> None:
    """Detach an IDE endpoint override from the persisted active auth profile.

    ``cfg`` is the per-session deep clone created by the bridge. A fresh profile
    name prevents stored profile credentials from shadowing the API key forwarded
    by the extension, while ``api_key_env`` keeps the secret outside the protocol.
    This helper deliberately does not save the cloned config.
    """

    profile = ProfileSpec(
        name=f"ide-native-{uuid.uuid4().hex}",
        protocol=OPENAI_COMPAT_PROTOCOL,
        base_url=base_url,
        api_key_env="ALYSIS_API_KEY",
        default_model=model,
    )
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)


def _ensure_chat_loop_facade() -> Any:
    """Inject the chat-loop globals the interactive CLI facade normally provides.

    ``cli_impl.chat.loop`` resolves a handful of helpers from its module
    globals (``_rebuild_session_tools_for_mode``,
    ``refresh_session_environment_context_message``, ...) that the
    interactive ``alysis chat`` startup copies in from the CLI module. The
    bridge reuses the loop's mutation primitives (``_apply_chat_effective_mode``,
    ``_apply_chat_persona``, ``_apply_config_menu_changes_to_session``) without
    that startup, so every session mutation from the IDE (Plan/Act switch,
    model picker, stream toggle) died with ``NameError`` and surfaced as
    "The IDE bridge encountered an unexpected internal error". ``setdefault``
    keeps a facade that already ran authoritative.
    """
    from ..agent.prompt_context import (
        refresh_session_environment_context_message,
        refresh_session_workspace_binding_context_message,
    )
    from ..cli_impl.chat import loop as chat_loop
    from ..cli_impl.commands.startup import _refresh_chat_hud_context_cache
    from ..cli_impl.commands.welcome import _rebuild_session_tools_for_mode

    loop_globals = chat_loop.__dict__
    loop_globals.setdefault("_rebuild_session_tools_for_mode", _rebuild_session_tools_for_mode)
    loop_globals.setdefault(
        "refresh_session_environment_context_message",
        refresh_session_environment_context_message,
    )
    loop_globals.setdefault(
        "refresh_session_workspace_binding_context_message",
        refresh_session_workspace_binding_context_message,
    )
    loop_globals.setdefault("_refresh_chat_hud_context_cache", _refresh_chat_hud_context_cache)
    return chat_loop


def _apply_agent_session_mode(agent_session: Any, mode: str) -> None:
    """Switch a bridge session's mode through the chat loop's mutation primitive."""
    chat_loop = _ensure_chat_loop_facade()
    chat_loop._apply_chat_effective_mode(
        session=agent_session,
        next_mode=mode,
        persist_default_mode=False,
    )


def _apply_agent_session_persona(agent_session: Any, *, persona: str, source: str) -> str:
    """Apply a persona through the chat loop's single mutation primitive.

    The bridge deliberately reuses ``_apply_chat_persona`` instead of
    reimplementing the clamp/write-scope/model-swap/restore logic; the same
    injection shim as ``_refresh_agent_session_config`` covers bridges
    embedded without the full CLI facade wiring.
    """
    chat_loop = _ensure_chat_loop_facade()
    return str(
        chat_loop._apply_chat_persona(
            session=agent_session,
            persona=persona,
            source=source,
        )
    )


def _apply_bridge_session_persona(agent_session: Any) -> str:
    """Start a new bridge session on the persona ``alysis chat`` would.

    The IDE creates each turn's session under the chat's own id, so the new
    session continues the chat's log. When that log records a persona it is
    applied again, as ``/resume`` does (``_reapply_resumed_persona``);
    otherwise the configured ``default_persona`` is applied once, as chat
    startup does (``_apply_startup_persona``). Applying the default every time
    would log it over the persona the chat switched to. Returns the source the
    session reports: "resume" or "config".
    """
    chat_loop = _ensure_chat_loop_facade()
    if chat_loop._load_resumed_persona(agent_session):
        chat_loop._reapply_resumed_persona(session=agent_session, console=None)
        return "resume"
    chat_loop._apply_startup_persona(session=agent_session, console=None)
    return "config"


def _attach_session_persona_registry(agent_session: Any, *, cfg: Any, root: Path) -> None:
    """Give bridge sessions the custom-persona registry interactive chat builds.

    ``create_session`` loads ``.alysis_personas`` only for interactive chat
    runtimes (the bridge creates sessions with ``non_interactive=True``), so
    the bridge attaches the registry itself. The loader keeps the chat path's
    discipline: fail-closed parsing, builtins never shadowed, custom persona
    bodies stay marked untrusted, and loader failures never break session
    creation.
    """
    if not persona_modes_enabled(cfg):
        return
    if getattr(agent_session, "persona_registry", None) is not None:
        return
    try:
        loaded, warnings = load_custom_personas(root)
    except Exception:  # noqa: BLE001 - custom personas must not break session create
        loaded, warnings = {}, ()
    with suppress(Exception):
        agent_session.persona_registry = dict(loaded) or None
        agent_session.persona_registry_warnings = tuple(warnings)


def _refresh_agent_session_config(agent_session: Any, cfg: Any) -> None:
    chat_loop = _ensure_chat_loop_facade()
    chat_loop._apply_config_menu_changes_to_session(session=agent_session, cfg=cfg)


def _resolve_workspace(
    path: Path,
    *,
    request_id: RequestId,
    allow_subprocess: bool = True,
) -> Any:
    try:
        return resolve_startup_workspace_binding(
            requested_path=path,
            interactive=False,
            create_if_missing=False,
            allow_broad_workspace=False,
            source="ide_protocol",
            action=WorkspaceAction.CHAT,
            console=None,
            allow_subprocess=allow_subprocess,
        )
    except WorkspaceBindingError as e:
        raise ProtocolError("workspace_rejected", str(e), request_id=request_id) from e


def _make_job_id() -> str:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"job_{ts}_{uuid.uuid4().hex[:8]}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _required_str(params: dict[str, Any], field: str, *, request_id: RequestId) -> str:
    value = params.get(field)
    if value is None or str(value).strip() == "":
        raise ProtocolError("missing_field", f"{field} is required.", request_id=request_id)
    if not isinstance(value, str):
        raise ProtocolError("invalid_field", f"{field} must be a string.", request_id=request_id)
    return value.strip()


def _approval_required_str(
    params: dict[str, Any],
    field: str,
    *,
    request_id: RequestId,
) -> str:
    value = params.get(field)
    if value is None or str(value).strip() == "":
        raise ProtocolError("invalid_request", f"{field} is required.", request_id=request_id)
    if not isinstance(value, str):
        raise ProtocolError("invalid_request", f"{field} must be a string.", request_id=request_id)
    return value.strip()


def _message_param(params: dict[str, Any], *, request_id: RequestId) -> str:
    if "message" in params:
        return _required_str(params, "message", request_id=request_id)
    if "instruction" in params:
        return _required_str(params, "instruction", request_id=request_id)
    raise ProtocolError("missing_field", "message is required.", request_id=request_id)


def _session_status_payload(session: BridgeSession) -> dict[str, Any]:
    cfg = getattr(session.agent_session, "cfg", None)
    sensitive_media = bool(session.sensitive_media)
    messages = getattr(session.agent_session, "messages", None)
    active_workdir = None
    active_workdir_relpath = None
    try:
        active_workdir = os.fspath(resolve_session_active_workdir_path(session.agent_session))
        active_workdir_relpath = resolve_session_active_workdir_relpath(session.agent_session)
    except Exception:  # noqa: BLE001 - status should remain best effort.
        pass
    return {
        "session_id": session.session_id,
        "workspace_root": os.fspath(session.root),
        "mode": session.mode,
        "persona": normalize_persona(
            getattr(session.agent_session, "persona", DEFAULT_PERSONA),
            getattr(session.agent_session, "persona_registry", None),
        ),
        "persona_source": session.persona_source,
        "closed": bool(session.closed),
        "active_job": _job_summary(session.active_job) if session.active_job else None,
        "last_job": _job_summary(session.last_job) if session.last_job else None,
        "model": str(getattr(cfg, "model", "") or ""),
        # The renderer-facing status projection must never receive the raw
        # endpoint selected for a sensitive Computer Use destination.
        "base_url": None
        if sensitive_media
        else str(redact_secrets(getattr(cfg, "base_url", "") or "")),
        "base_url_redacted": sensitive_media,
        "temperature": getattr(cfg, "temperature", None),
        "stream": bool(getattr(cfg, "stream", False)),
        "max_steps": int(getattr(cfg, "max_steps", 0) or 0),
        "no_log": bool(getattr(session.agent_session, "no_log", False)),
        "yes": bool(getattr(session.agent_session, "yes", False)),
        "subagents_enabled": bool(getattr(cfg, "subagents_enabled", False)),
        "active_workdir": active_workdir,
        "active_workdir_relpath": active_workdir_relpath,
        "effective_verification_commands": _list_of_strings(
            getattr(session.agent_session, "verify_cmd", getattr(cfg, "verify_commands", []))
        ),
        "message_count": len(messages) if isinstance(messages, list) else 0,
        "pending_images": len(session.pending_images),
        "pending_approvals": len(session.pending_approvals),
        "pending_host_actions": len(session.pending_host_actions),
        "host_actions": sorted(session.host_actions),
        "workspace_trusted": session.workspace_trusted,
        "sensitive_media": sensitive_media,
        **(
            {"route_fingerprint": session.computer_use_destination.route_fingerprint}
            if sensitive_media and session.computer_use_destination is not None
            else {}
        ),
    }


def _ordered_persona_definitions(registry: Any) -> list[Any]:
    """Builtins in definition order, then customs sorted by name.

    The same ordering the TUI persona picker and ``next_persona`` cycling use.
    """
    personas = all_personas(registry)
    order = list(PERSONA_NAMES)
    if registry:
        order += sorted(str(name) for name in registry if name not in BUILTIN_PERSONAS)
    return [personas[name] for name in order if name in personas]


def _persona_definition_payload(definition: Any) -> dict[str, Any]:
    # Deliberately excludes overlay_prompt and prompt_trust: the IDE renders
    # names and postures, never prompt bodies. The wire vocabulary for
    # source_scope is fixed at builtin|custom; the loader's finer-grained
    # project/user provenance both map to custom.
    source_scope = str(definition.source_scope)
    return {
        "name": str(definition.name),
        "description": str(definition.description),
        "default_exec_mode": str(definition.default_exec_mode),
        "model_role": str(definition.model_role),
        "source_scope": "builtin" if source_scope == "builtin" else "custom",
        "allow_write_globs": [str(glob) for glob in definition.allow_write_globs],
    }


def _session_subagents_payload(session: BridgeSession) -> dict[str, Any]:
    cfg = getattr(session.agent_session, "cfg", None)
    registry = getattr(session.agent_session, "subagent_registry", None)
    available = sorted(str(name) for name in registry) if isinstance(registry, dict) else []
    enabled = bool(getattr(session.agent_session, "subagents_enabled", False))
    if cfg is not None:
        enabled = bool(getattr(cfg, "subagents_enabled", enabled))
    return {
        "session_id": session.session_id,
        "enabled": enabled,
        "available": available,
        "available_count": len(available),
        "explicit_execution_supported": False,
        "explicit_execution_policy": "IDE v1 supports status/toggle only; explicit subagent execution remains CLI-only until a separate approval and lifecycle model exists.",
        "lifecycle_event": "subagent_state_changed",
        "execution_lifecycle": "in_turn_parent_owned",
        "cancellation": "parent_job",
        "independently_resumable": False,
        "background_worker_surface": "forge.swarm",
        "forge_execute_policy": "Forge Execute v1 keeps subagents disabled and review-mode scoped.",
        "secret_values_included": False,
    }


def _trace_level_param(params: dict[str, Any], *, request_id: RequestId) -> str:
    level = str(params.get("level") or "").strip().lower()
    if level not in TRACE_LEVELS:
        raise ProtocolError(
            "invalid_trace_level",
            "Trace level must be off, compact, or full.",
            request_id=request_id,
        )
    return level


def _session_trace_level(session: BridgeSession) -> str:
    level = str(getattr(session.surface, "trace_level", "") or "").strip().lower()
    return level if level in TRACE_LEVELS else "compact"


def _set_session_trace_level(session: BridgeSession, level: str) -> str:
    setter = getattr(session.surface, "set_trace_level", None)
    if callable(setter):
        applied = str(setter(level) or "").strip().lower()
    else:
        session.surface.trace_level = level
        applied = level
    if applied not in TRACE_LEVELS:
        applied = "compact"
    agent_surface = getattr(session.agent_session, "surface", None)
    if agent_surface is not session.surface:
        agent_setter = getattr(agent_surface, "set_trace_level", None)
        if callable(agent_setter):
            agent_setter(applied)
        elif agent_surface is not None:
            with suppress(Exception):
                agent_surface.trace_level = applied
    return applied


def _session_reasoning_trace_events(
    params: dict[str, Any],
    *,
    sensitive_media: bool,
    request_id: RequestId,
) -> bool:
    """Whether a new session streams reasoning events: host opt-in, never Computer Use."""

    requested = _optional_bool(params, "reasoning_trace", default=False, request_id=request_id)
    return requested and not sensitive_media


def _session_trace_status_payload(session: BridgeSession, retained_events: int) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "supported": True,
        "level": _session_trace_level(session),
        "reasoning_events": bool(getattr(session.surface, "reasoning_trace_events", False)),
        "levels": sorted(TRACE_LEVELS),
        "retained_events": retained_events,
        "redacted": True,
        "secret_values_included": False,
        "max_events": TRACE_EVENT_RESPONSE_MAX,
        "max_bytes": TRACE_ARTIFACT_MAX_BYTES,
        "full_trace_requires_confirmation": True,
        "raw_provider_headers_included": False,
        "environment_variables_included": False,
    }


def _bounded_trace_events(
    events: list[dict[str, Any]],
    *,
    max_events: int,
    max_bytes: int,
    after_sequence: int | None,
) -> dict[str, Any]:
    if after_sequence is None:
        selected_all = events[-max_events:]
        truncated_by_event_count = len(events) > len(selected_all)
    else:
        selected_after = [
            event for event in events if int(event.get("sequence") or 0) > after_sequence
        ]
        selected_all = selected_after[:max_events]
        truncated_by_event_count = len(selected_after) > len(selected_all)
    selected: list[dict[str, Any]] = []
    bytes_used = 0
    truncated_by_bytes = False
    for event in selected_all:
        safe_event = redact_secrets(_json_clone(event))
        encoded = json.dumps(safe_event, ensure_ascii=True, sort_keys=True).encode(
            "utf-8",
            errors="replace",
        )
        if selected and bytes_used + len(encoded) > max_bytes:
            truncated_by_bytes = True
            break
        if not selected and len(encoded) > max_bytes:
            truncated_by_bytes = True
            break
        selected.append(safe_event)
        bytes_used += len(encoded)
    return {
        "events": selected,
        "count": len(selected),
        "total_retained": len(events),
        "bytes": bytes_used,
        "truncated": truncated_by_event_count or truncated_by_bytes,
        "truncated_by_event_count": truncated_by_event_count,
        "truncated_by_bytes": truncated_by_bytes,
        "lowest_retained_sequence": int(events[0]["sequence"]) if events else None,
        "highest_retained_sequence": int(events[-1]["sequence"]) if events else None,
    }


def _terminal_manager(session: BridgeSession) -> Any | None:
    manager = getattr(session.agent_session, "terminal_manager", None)
    if manager is None:
        return None
    if not callable(getattr(manager, "list", None)):
        return None
    return manager


def _terminals_unavailable_payload(session: BridgeSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "supported": False,
        "available": False,
        "reason": "Background terminals are unavailable because this session has no terminal manager.",
        "terminals": [],
        "count": 0,
        "redacted": True,
        "secret_values_included": False,
        "arbitrary_shell_execution": False,
        "interactive_pty_streaming": False,
    }


def _terminal_summary_payload(summary: Any, root: Path) -> dict[str, Any]:
    cwd = Path(getattr(summary, "cwd", root))
    cwd_text = os.fspath(cwd)
    try:
        cwd_rel = cwd.resolve().relative_to(root.resolve()).as_posix()
    except Exception:  # noqa: BLE001 - terminal summary remains best effort.
        cwd_rel = None
    return {
        "process_id": str(redact_secrets(str(getattr(summary, "process_id", "") or ""))),
        "cmd": str(redact_secrets(str(getattr(summary, "cmd", "") or "")))[:500],
        "cwd": str(redact_secrets(cwd_text)),
        "cwd_relpath": cwd_rel,
        "status": str(getattr(summary, "status", "") or ""),
        "exit_code": _non_negative_int_or_none(getattr(summary, "exit_code", None)),
        "runtime_s": _float_or_none(getattr(summary, "runtime_s", None)),
        "started_at": _wall_timestamp_iso(getattr(summary, "started_at_wall", None)),
    }


def _terminal_snapshot_payload(
    session: BridgeSession,
    snapshot: Any,
    *,
    max_lines: int,
    max_bytes: int,
) -> dict[str, Any]:
    lines_raw = list(getattr(snapshot, "lines", ()) or ())
    lines: list[dict[str, Any]] = []
    bytes_used = 0
    truncated_by_bytes = False
    selected_raw = lines_raw[-max_lines:]
    truncated_by_line_count = len(lines_raw) > len(selected_raw)
    for line in selected_raw:
        text = str(redact_secrets(str(getattr(line, "text", "") or "")))
        encoded_len = len(text.encode("utf-8", errors="replace"))
        if lines and bytes_used + encoded_len > max_bytes:
            truncated_by_bytes = True
            break
        if not lines and encoded_len > max_bytes:
            text_bytes = text.encode("utf-8", errors="replace")[:max_bytes]
            text = text_bytes.decode("utf-8", errors="replace")
            encoded_len = len(text.encode("utf-8", errors="replace"))
            truncated_by_bytes = True
        bytes_used += encoded_len
        lines.append(
            {
                "seq": int(getattr(line, "seq", 0) or 0),
                "stream": str(getattr(line, "stream", "") or ""),
                "text": text,
                "ts": _wall_timestamp_iso(getattr(line, "ts", None)),
            }
        )
    return {
        "session_id": session.session_id,
        "supported": True,
        "available": True,
        "process_id": str(redact_secrets(str(getattr(snapshot, "process_id", "") or ""))),
        "status": str(getattr(snapshot, "status", "") or ""),
        "exit_code": _non_negative_int_or_none(getattr(snapshot, "exit_code", None)),
        "failure_reason": redact_secrets(getattr(snapshot, "failure_reason", None)),
        "lines": lines,
        "line_count": len(lines),
        "next_seq": int(getattr(snapshot, "next_seq", 0) or 0),
        "dropped_lines": int(getattr(snapshot, "dropped_lines", 0) or 0),
        "runtime_s": _float_or_none(getattr(snapshot, "runtime_s", None)),
        "started_at": _wall_timestamp_iso(getattr(snapshot, "started_at_wall", None)),
        "total_bytes": int(getattr(snapshot, "total_bytes", 0) or 0),
        "bytes": bytes_used,
        "max_lines": max_lines,
        "max_bytes": max_bytes,
        "truncated": truncated_by_line_count or truncated_by_bytes,
        "truncated_by_line_count": truncated_by_line_count,
        "truncated_by_bytes": truncated_by_bytes,
        "redacted": True,
        "secret_values_included": False,
        "arbitrary_shell_execution": False,
        "interactive_pty_streaming": False,
    }


def _wall_timestamp_iso(value: Any) -> str | None:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _optional_int_attr(value: Any, attr: str) -> int | None:
    raw = getattr(value, attr, None)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _optional_bool_attr(value: Any, attr: str) -> bool | None:
    raw = getattr(value, attr, None)
    return raw if isinstance(raw, bool) else None


def _required_bool(params: dict[str, Any], field: str, *, request_id: RequestId) -> bool:
    if field not in params:
        raise ProtocolError("missing_field", f"{field} is required.", request_id=request_id)
    value = params[field]
    if not isinstance(value, bool):
        raise ProtocolError("invalid_field", f"{field} must be a boolean.", request_id=request_id)
    return value


def _approval_required_bool(
    params: dict[str, Any],
    field: str,
    *,
    request_id: RequestId,
) -> bool:
    if field not in params:
        raise ProtocolError("invalid_request", f"{field} is required.", request_id=request_id)
    value = params[field]
    if not isinstance(value, bool):
        raise ProtocolError(
            "invalid_request",
            f"{field} must be a boolean.",
            request_id=request_id,
        )
    return value


def _optional_bool(
    params: dict[str, Any],
    field: str,
    *,
    default: bool,
    request_id: RequestId,
) -> bool:
    if field not in params:
        return default
    value = params[field]
    if not isinstance(value, bool):
        raise ProtocolError("invalid_field", f"{field} must be a boolean.", request_id=request_id)
    return value


def _optional_bool_or_none(
    params: dict[str, Any],
    field: str,
    *,
    request_id: RequestId,
) -> bool | None:
    if field not in params:
        return None
    value = params[field]
    if not isinstance(value, bool):
        raise ProtocolError("invalid_field", f"{field} must be a boolean.", request_id=request_id)
    return value


def _optional_str(params: dict[str, Any], field: str, *, request_id: RequestId) -> str | None:
    value = params.get(field)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ProtocolError("invalid_field", f"{field} must be a string.", request_id=request_id)
    clean = value.strip()
    return clean or None


def _session_log_dir_param(
    params: dict[str, Any],
    *,
    request_id: RequestId,
) -> Path | None:
    raw = _optional_str(params, "session_dir", request_id=request_id)
    if raw is None:
        return None
    if "\x00" in raw:
        raise ProtocolError(
            "invalid_session_dir",
            "session_dir contains an unsupported null byte.",
            request_id=request_id,
        )
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ProtocolError(
            "invalid_session_dir",
            "session_dir must be an absolute path controlled by the IDE host.",
            request_id=request_id,
        )
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ProtocolError(
            "invalid_session_dir",
            f"session_dir could not be resolved: {exc}",
            request_id=request_id,
        ) from exc
    if resolved.exists() and not resolved.is_dir():
        raise ProtocolError(
            "invalid_session_dir",
            "session_dir must identify a directory.",
            request_id=request_id,
        )

    trusted_root_raw = str(env_get(IDE_SESSION_DIR_ROOT_ENV) or "").strip()
    if trusted_root_raw:
        trusted_root_candidate = Path(trusted_root_raw)
        if not trusted_root_candidate.is_absolute():
            raise ProtocolError(
                "invalid_session_dir_root",
                f"{IDE_SESSION_DIR_ROOT_ENV} must be an absolute path.",
                request_id=request_id,
            )
        try:
            trusted_root = trusted_root_candidate.resolve(strict=False)
            resolved.relative_to(trusted_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProtocolError(
                "session_dir_outside_trusted_root",
                f"session_dir must stay inside {IDE_SESSION_DIR_ROOT_ENV}.",
                request_id=request_id,
            ) from exc
    return resolved


def _source_path_param(params: dict[str, Any], *, request_id: RequestId) -> str:
    value = _optional_str(params, "source_path", request_id=request_id)
    if value is not None:
        return value
    return _required_str(params, "source", request_id=request_id)


def _param_is_present(params: dict[str, Any], field: str) -> bool:
    return field in params and params[field] is not None


def _positive_int_param(
    params: dict[str, Any],
    field: str,
    *,
    default: int,
    request_id: RequestId,
    upper: int | None = None,
) -> int:
    if field not in params or params[field] is None:
        return default
    try:
        value = int(params[field])
    except (TypeError, ValueError) as exc:
        raise ProtocolError(
            "invalid_field", f"{field} must be a positive integer.", request_id=request_id
        ) from exc
    if value <= 0:
        raise ProtocolError(
            "invalid_field", f"{field} must be a positive integer.", request_id=request_id
        )
    if upper is not None:
        value = min(value, upper)
    return value


def _non_negative_int_param(
    params: dict[str, Any],
    field: str,
    *,
    default: int,
    request_id: RequestId,
) -> int:
    if field not in params or params[field] is None:
        return default
    raw = params[field]
    if isinstance(raw, bool):
        raise ProtocolError(
            "invalid_field", f"{field} must be a non-negative integer.", request_id=request_id
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(
            "invalid_field", f"{field} must be a non-negative integer.", request_id=request_id
        ) from exc
    if value < 0:
        raise ProtocolError(
            "invalid_field", f"{field} must be a non-negative integer.", request_id=request_id
        )
    return value


def _verify_commands_param(params: dict[str, Any], *, request_id: RequestId) -> list[str] | None:
    raw: Any
    if "verify_commands" in params:
        raw = params.get("verify_commands")
    elif "verify_cmd" in params:
        raw = params.get("verify_cmd")
    else:
        return None
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if isinstance(raw, list):
        commands: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise ProtocolError(
                    "invalid_field",
                    "verify_commands must contain only strings.",
                    request_id=request_id,
                )
            text = item.strip()
            if text:
                commands.append(text)
        return commands
    raise ProtocolError(
        "invalid_field",
        "verify_cmd must be a string or verify_commands must be an array of strings.",
        request_id=request_id,
    )


def _reject_unsupported_chat_send_fields(
    params: dict[str, Any],
    *,
    request_id: RequestId,
) -> None:
    unsupported = sorted(set(params) - CHAT_SEND_ALLOWED_FIELDS)
    if unsupported:
        raise ProtocolError(
            "unsupported_turn_option",
            "chat.send only accepts message/instruction and approved image paths for an existing session.",
            request_id=request_id,
        )


def _reject_unsupported_run_start_fields(
    params: dict[str, Any],
    *,
    request_id: RequestId,
) -> None:
    unsupported = sorted(set(params) - RUN_START_ALLOWED_FIELDS)
    if unsupported:
        raise ProtocolError(
            "unsupported_run_option",
            "run.start received unsupported option(s): " + ", ".join(unsupported),
            request_id=request_id,
        )


def _image_paths_param(
    params: dict[str, Any],
    *,
    workspace_root: Path,
    request_id: RequestId,
) -> list[str]:
    raw = params.get("images", params.get("image_paths"))
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ProtocolError(
            "invalid_field",
            "images must be an array of workspace-relative paths.",
            request_id=request_id,
        )
    resolved: list[str] = []
    root = workspace_root.resolve()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ProtocolError(
                "invalid_field",
                "images must contain non-empty strings.",
                request_id=request_id,
            )
        candidate = Path(item).expanduser()
        candidate_was_absolute = candidate.is_absolute()
        if not candidate_was_absolute:
            candidate = root / candidate
        if candidate.is_symlink():
            raise ProtocolError(
                "image_symlink_rejected",
                "Image paths may not be symlinks.",
                request_id=request_id,
            )
        resolved_path = candidate.resolve()
        try:
            resolved_path.relative_to(root)
        except ValueError as exc:
            if not candidate_was_absolute:
                raise ProtocolError(
                    "image_path_outside_workspace",
                    "Relative image paths must stay inside the resolved workspace.",
                    request_id=request_id,
                ) from exc
            try:
                host_image = validate_ide_host_image(
                    item,
                    max_bytes=MAX_IDE_IMAGE_BYTES,
                    supported_mime_types=SUPPORTED_IDE_IMAGE_MIME_TYPES,
                )
            except HostImageValidationError as host_exc:
                if host_exc.code == "host_image_root_missing":
                    raise ProtocolError(
                        "image_path_outside_workspace",
                        "Image paths outside the workspace require a configured secure host image root.",
                        request_id=request_id,
                    ) from host_exc
                raise ProtocolError(
                    host_exc.code,
                    host_exc.message,
                    request_id=request_id,
                ) from host_exc
            resolved.append(os.fspath(host_image.path))
            continue
        if not resolved_path.exists():
            raise ProtocolError(
                "image_not_found", "Image path does not exist.", request_id=request_id
            )
        if not resolved_path.is_file():
            raise ProtocolError(
                "image_not_file", "Image path must be a regular file.", request_id=request_id
            )
        try:
            size = resolved_path.stat().st_size
        except OSError as exc:
            raise ProtocolError(
                "image_not_found", "Image path could not be inspected.", request_id=request_id
            ) from exc
        if size > MAX_IDE_IMAGE_BYTES:
            raise ProtocolError(
                "image_too_large",
                f"Image exceeds the maximum supported size ({MAX_IDE_IMAGE_BYTES} bytes).",
                request_id=request_id,
            )
        mime, _encoding = mimetypes.guess_type(os.fspath(resolved_path))
        if mime not in SUPPORTED_IDE_IMAGE_MIME_TYPES:
            raise ProtocolError(
                "unsupported_image_type",
                "Unsupported image type.",
                request_id=request_id,
            )
        resolved.append(os.fspath(resolved_path))
    return resolved


def _image_entry(path: str, *, workspace_root: Path) -> dict[str, Any]:
    resolved_path = Path(path).resolve()
    host_screen = is_configured_host_image_candidate(path, workspace_root=workspace_root)
    try:
        relpath = os.fspath(resolved_path.relative_to(workspace_root.resolve()))
    except ValueError:
        relpath = None
    try:
        size_bytes = resolved_path.stat().st_size
    except OSError:
        size_bytes = None
    mime, _encoding = mimetypes.guess_type(os.fspath(resolved_path))
    payload: dict[str, Any] = {
        "relpath": relpath,
        "mime_type": mime,
        "size_bytes": size_bytes,
    }
    if host_screen:
        payload["source"] = "host-screen"
    else:
        payload["path"] = os.fspath(resolved_path)
    return payload


def _session_images_payload(session: BridgeSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "images": [
            _image_entry(path, workspace_root=session.root) for path in session.pending_images
        ],
        "count": len(session.pending_images),
        "max_bytes": MAX_IDE_IMAGE_BYTES,
        "binary_jsonl": False,
    }


def _dedupe_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for path in paths:
        clean = os.fspath(Path(path).resolve())
        if clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _remove_pending_images(session: BridgeSession, used_paths: list[str]) -> None:
    used = set(_dedupe_paths(used_paths))
    session.pending_images = [
        path for path in session.pending_images if os.fspath(Path(path).resolve()) not in used
    ]


def _session_messages(agent_session: Any) -> list[dict[str, Any]]:
    messages = getattr(agent_session, "messages", None)
    if not isinstance(messages, list):
        return []
    return [message for message in messages if isinstance(message, dict)]


def _session_tool_list(agent_session: Any) -> list[dict[str, Any]] | None:
    tool_list = getattr(agent_session, "tool_list", None)
    if not isinstance(tool_list, list):
        return None
    return [tool for tool in tool_list if isinstance(tool, dict)]


def _messages_fingerprint(messages: list[dict[str, Any]]) -> str:
    return json.dumps(messages, sort_keys=True, ensure_ascii=True, default=str)


def _pinned_prefix_len(agent_session: Any, messages: list[dict[str, Any]]) -> int:
    try:
        pinned_prefix_len = int(getattr(agent_session, "pinned_prefix_len", 0) or 0)
    except (TypeError, ValueError):
        pinned_prefix_len = 0
    compactor = getattr(agent_session, "conversation_compactor", None)
    state = getattr(compactor, "state", None)
    if pinned_prefix_len <= 0 and state is not None:
        try:
            pinned_prefix_len = int(getattr(state, "pinned_prefix_len", 0) or 0)
        except (TypeError, ValueError):
            pinned_prefix_len = 0
    return max(0, min(pinned_prefix_len, len(messages)))


def _call_context_left(agent_session: Any) -> Any | None:
    context_left = getattr(agent_session, "context_left", None)
    if not callable(context_left):
        return None
    try:
        return context_left()
    except Exception:  # noqa: BLE001 - context remains best-effort in status-like calls.
        return None


def _non_negative_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_FEEDBACK_STATUS_CODES = {
    404: "feedback_unavailable",
    409: "feedback_conflict",
    413: "feedback_too_large",
    429: "feedback_rate_limited",
}


def _feedback_delivery_code(exc: FeedbackReportError) -> str:
    """The protocol error code for a refused or unconfirmed report."""
    if not isinstance(exc, feedback_delivery.FeedbackDeliveryError):
        return "feedback_delivery_failed"
    if exc.status_code == 401:
        return "feedback_sign_in_expired" if exc.signed_in else "feedback_sign_in_required"
    return _FEEDBACK_STATUS_CODES.get(exc.status_code, "feedback_delivery_failed")


@dataclass(frozen=True)
class _RetainedFeedbackSession:
    """The messages of a chat that is not open, as feedback diagnostics read them."""

    messages: list[dict[str, Any]]


def _retained_session_messages(
    params: dict[str, Any], session_id: str, *, request_id: RequestId
) -> list[dict[str, Any]]:
    """Read a retained chat log the way session.resume rebuilds it; nothing is written."""
    from ..cli_impl.commands.chat_resume_helpers import (
        _load_chat_resume_messages,
        _normalize_chat_resume_session_id,
        _resolve_chat_resume_session_path,
    )

    normalized = _normalize_chat_resume_session_id(session_id)
    if normalized is None:
        raise ProtocolError(
            "invalid_session_id",
            "session_id contains unsupported characters.",
            request_id=request_id,
        )
    sessions_dir = _session_log_dir_param(params, request_id=request_id) or resolve_sessions_dir(
        load_config()
    )
    path = _resolve_chat_resume_session_path(sessions_dir=sessions_dir, session_id=normalized)
    if path is None:
        raise ProtocolError("session_not_found", "Session was not found.", request_id=request_id)
    return _load_chat_resume_messages(path)


def _session_log_dir(agent_session: Any) -> Path:
    store = getattr(agent_session, "store", None)
    sessions_dir = getattr(store, "sessions_dir", None)
    if sessions_dir is not None:
        return Path(sessions_dir)
    cfg = getattr(agent_session, "cfg", None)
    if cfg is not None:
        return resolve_sessions_dir(cfg)
    return resolve_sessions_dir(load_config())


def _timestamp_iso(value: float) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    except (TypeError, ValueError, OverflowError):
        return _now_iso()


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _make_context_ignore_predicate(root: Path) -> Callable[[Path], bool]:
    resolved_root = root.resolve()
    cache: dict[str, bool] = {}

    def _is_ignored(path: Path) -> bool:
        try:
            relative = path.resolve(strict=False).relative_to(resolved_root).as_posix()
        except ValueError:
            return True
        if relative in cache:
            return cache[relative]
        if relative == "." or relative.split("/", 1)[0] in {".git", ".alysis"}:
            cache[relative] = True
            return True
        cache[relative] = relative in _git_check_ignored(resolved_root, [relative])
        return cache[relative]

    return _is_ignored


def _prompt_queue_item_payload(
    item: PromptQueueItem, *, include_preview: bool = False
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": item.session_id,
        "prompt_id": item.prompt_id,
        "sequence": item.sequence,
        "state": item.state.value,
        "created_at": _timestamp_iso(item.created_at),
        "updated_at": _timestamp_iso(item.updated_at),
        "attempts": item.attempts,
        "terminal_at": (_timestamp_iso(item.terminal_at) if item.terminal_at is not None else None),
        "error_code": item.error_code,
    }
    if include_preview:
        message = item.payload.get("message")
        preview = str(redact_secrets(message)) if isinstance(message, str) else ""
        payload["message_preview"] = preview[:500]
        payload["message_truncated"] = len(preview) > 500
    return payload


def _checkpoint_payload(checkpoint: Checkpoint) -> dict[str, Any]:
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "session_id": checkpoint.session_id,
        "turn_id": checkpoint.turn_id,
        "step_id": checkpoint.step_id,
        "parent_id": checkpoint.parent_id,
        "kind": checkpoint.kind,
        "created_at": checkpoint.created_at,
        "message": checkpoint.message,
        "changes": [record.__dict__ for record in checkpoint.changes],
        "omitted_paths": list(checkpoint.omitted_paths),
        "non_revertible_paths": list(checkpoint.non_revertible_paths),
        "fully_revertible": not checkpoint.non_revertible_paths,
        "reverts_id": checkpoint.reverts_id,
        "redoes_id": checkpoint.redoes_id,
    }


def _permission_evaluation_payload(evaluation: Any) -> dict[str, Any]:
    return {
        "decision": evaluation.decision.value,
        "reason": evaluation.reason,
        "matched_rule_id": evaluation.matched_rule_id,
        "matched_rule_source": evaluation.matched_rule_source,
        "specificity": evaluation.specificity,
    }


def _permission_path_safety_flags(
    paths: list[str], workspace_root: str | os.PathLike[str] | None
) -> tuple[bool, bool]:
    """Detect sensitive and symlink-escaped targets before policy matching.

    ``PermissionPolicy`` also performs platform-neutral lexical normalization,
    but this bridge runs on the filesystem that will execute the operation and
    can therefore resolve existing symlinks.  Without this host-side check, a
    workspace-relative symlink to an external directory could bypass the
    non-allowable external-directory safety override.
    """

    sensitive = any(classify_sensitive_path(path).sensitive for path in paths)
    if workspace_root is None:
        return sensitive, False
    try:
        root = Path(workspace_root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        # Invalid roots are handled by policy validation. Do not infer that an
        # arbitrary path is safe when host resolution itself failed.
        return sensitive, True
    external = False
    for raw_path in paths:
        try:
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            external = True
            continue
        sensitive = sensitive or classify_sensitive_path(resolved).sensitive
    return sensitive, external


def _append_prompt_store_event(
    session: BridgeSession,
    event_type: str,
    prompt_id: str | None,
    *,
    checkpoint_id: str | None = None,
) -> bool:
    if not prompt_id:
        return False
    store = getattr(session.agent_session, "store", None)
    append = getattr(store, "append", None)
    if not callable(append):
        return False
    payload: dict[str, Any] = {"prompt_id": prompt_id}
    if checkpoint_id:
        payload["checkpoint_id"] = checkpoint_id
    try:
        append(event_type, payload)
    except Exception:
        return False
    return True


def _checkpointing_supported(session: BridgeSession) -> bool:
    store = getattr(session.agent_session, "store", None)
    return callable(getattr(store, "events_snapshot", None)) and callable(
        getattr(store, "append", None)
    )


# The restore baseline reads every file in the workspace (twice, to detect files changing
# under it) and shells out to git. On Windows with real-time antivirus scanning each file open
# costs milliseconds: a 1,100-file repository measured 20-24 s. It starts before the first
# action that may change files (text-only replies never need it), and that action waits for
# it, cancellably and with a visible "Preparing restore points" activity, instead of being
# refused. The deadline only bounds a worker that never finishes; reads stay available.
_CHANGE_LEDGER_SETUP_DEADLINE_SECONDS = 300.0
_CHANGE_LEDGER_SETUP_NOTICE_SECONDS = 1.0


def _start_change_ledger_setup(
    session: BridgeSession,
) -> tuple[threading.Event, dict[str, Any]]:
    """Start the one shared baseline worker unless one is already running; never waits."""

    setup = getattr(session, "checkpoint_setup", None)
    if setup is not None:
        return setup
    existing_ledger = session.change_ledger
    session.change_ledger = None
    outcome: dict[str, Any] = {}
    done = threading.Event()
    session.checkpoint_setup = (done, outcome)

    def _build() -> None:
        try:
            ledger = existing_ledger if existing_ledger is not None else ChangeLedger(session.root)
            ledger.ensure_baseline(session.session_id)
            outcome["ledger"] = ledger
        except Exception as exc:  # noqa: BLE001 - reported and retried on the next request
            outcome["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_build, name="alysis-ledger-init", daemon=True).start()
    return done, outcome


def _emit_restore_point_activity(session: BridgeSession, status: str) -> None:
    emit_activity = getattr(session.surface, "emit_activity", None)
    if not callable(emit_activity):
        return
    emit_activity(
        ActivityEvent(
            activity_id=f"restore-points-{session.session_id}"[:128],
            kind="git",
            operation="prepare_restore_points",
            display_title="Preparing restore points",
            status=status,  # type: ignore[arg-type]
            summary=("Saving the project's current state once per chat, so changes can be undone."),
        )
    )


def _prepare_change_ledger_with_deadline(
    session: BridgeSession,
    *,
    deadline_seconds: float = _CHANGE_LEDGER_SETUP_DEADLINE_SECONDS,
) -> bool:
    """Wait a bounded, cancellable time for the one shared baseline worker.

    The worker is started at most once per setup and outlives a timed-out wait, so a later
    call (in the same request or the next) adopts its result. The dispatch guard blocks
    writes until a caller has adopted a completed baseline. A wait longer than
    ``_CHANGE_LEDGER_SETUP_NOTICE_SECONDS`` is shown as a "Preparing restore points" activity.
    """
    done, outcome = _start_change_ledger_setup(session)
    started = time.monotonic()
    deadline = started + max(0.0, deadline_seconds)
    announced = False
    while True:
        job = getattr(session, "active_job", None)
        if job is not None:
            _check_job_cancelled(job)
        remaining = deadline - time.monotonic()
        if done.wait(timeout=max(0.0, min(0.1, remaining))):
            break
        if not announced and time.monotonic() - started >= _CHANGE_LEDGER_SETUP_NOTICE_SECONDS:
            _emit_restore_point_activity(session, "running")
            announced = True
        if remaining <= 0:
            if announced:
                _emit_restore_point_activity(session, "blocked")
            session.surface.emit_warning(
                "Restore snapshots are still being prepared. Actions that may change files "
                "are paused; file reading and Git review remain available. "
                "Wait a moment, then send your request again."
            )
            return False
    session.checkpoint_setup = None
    error = outcome.get("error")
    if error is not None:
        if announced:
            _emit_restore_point_activity(session, "failed")
        session.surface.emit_warning(
            f"Restore snapshots are unavailable; actions that may change files are paused: {error}"
        )
        return False
    if announced:
        _emit_restore_point_activity(session, "succeeded")
    session.change_ledger = outcome["ledger"]
    return True


def _prompt_completed_in_store(session: BridgeSession, prompt_id: str) -> bool:
    return _prompt_event_in_store(session, prompt_id, "ide_prompt_completed")


def _prompt_started_in_store(session: BridgeSession, prompt_id: str) -> bool:
    return _prompt_event_in_store(session, prompt_id, "ide_prompt_started")


def _prompt_event_in_store(session: BridgeSession, prompt_id: str, event_type: str) -> bool:
    store = getattr(session.agent_session, "store", None)
    snapshot = getattr(store, "events_snapshot", None)
    if not callable(snapshot):
        return False
    try:
        events = snapshot()
    except Exception:
        return False
    if not isinstance(events, list):
        return False
    for event in reversed(events):
        if not isinstance(event, dict) or event.get("type") != event_type:
            continue
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("prompt_id") == prompt_id:
            return True
    return False


def _message_content_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else message
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list | tuple | dict):
        try:
            return json.dumps(content, ensure_ascii=True, sort_keys=True)
        except (TypeError, ValueError):
            return str(content)
    return str(content)


def _redact_resume_message(value: Any) -> Any:
    if isinstance(value, str):
        return str(redact_secrets(value))
    if isinstance(value, list):
        return [_redact_resume_message(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_resume_message(item) for key, item in value.items()}
    return value


def _reconcile_session_job_state(session: BridgeSession) -> None:
    job = session.active_job
    if job is not None and job.status in FINAL_JOB_STATUSES:
        session.last_job = job
        session.active_job = None


def _hold_unread_steers(session: BridgeSession) -> None:
    """Move steering left in the inbox to the session's unread list (newest 16 kept)."""
    inbox = steer_inbox_for(session.agent_session)
    if inbox is None:
        return
    unread = inbox.drain()
    if unread:
        session.unread_steers.extend(unread)
        del session.unread_steers[:-MAX_PENDING_STEER_MESSAGES]


def _job_is_active(job: BridgeJob | None) -> bool:
    return job is not None and job.status in ACTIVE_JOB_STATUSES


def _mark_job_running(job: BridgeJob) -> None:
    if job.status in FINAL_JOB_STATUSES:
        return
    now = _now_iso()
    if job.status != "cancellation_requested":
        job.status = "running"
    job.started_at = job.started_at or now
    job.updated_at = now


def _mark_job_completed(
    job: BridgeJob,
    *,
    status: str,
    exit_code: int | None = None,
    error: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    if job.status in FINAL_JOB_STATUSES:
        # Idempotent terminal callers (shutdown/cancel races) must not retain a
        # descriptor merely because another path finalized the job first.
        job.host_image_binding = None
        return
    now = _now_iso()
    job.status = status
    job.updated_at = now
    job.completed_at = now
    job.exit_code = exit_code
    if error is not None:
        job.error = str(redact_secrets(error))
    if result is not None:
        clean_result = redact_secrets(result)
        job.result = clean_result if isinstance(clean_result, dict) else None
    # ``last_job`` and retained job history are intentionally diagnostic only.
    # Never retain the filesystem capability used to validate host pixels.
    job.host_image_binding = None


def _request_job_cancellation(job: BridgeJob, reason: str) -> dict[str, Any]:
    clean_reason = str(redact_secrets(reason or "cancelled_by_user"))
    now = _now_iso()
    if job.status == "cancelled":
        return {
            "status": "cancelled",
            "state": "cancelled",
            "already_terminal": True,
            "job": _job_summary(job),
        }
    if job.status in {"completed", "failed"}:
        return {
            "status": job.status,
            "state": job.status,
            "already_terminal": True,
            "job": _job_summary(job),
        }
    if not job.cancellable:
        return {
            "status": "non_cancellable",
            "state": job.status,
            "job": _job_summary(job),
        }
    job.cancellation_event.set()
    job.cancellation_reason = clean_reason
    job.cancellation_requested_at = job.cancellation_requested_at or now
    # Cancel revokes future acquisition of this one-shot capability. A worker
    # that already captured it still observes the same cancellation token before
    # entering the provider call; the descriptor itself is never retained in
    # bridge job history while cancellation is pending.
    job.host_image_binding = None
    if job.status != "cancellation_requested":
        job.status = "cancellation_requested"
    job.updated_at = now
    return {
        "status": "cancellation_requested",
        "state": "cancellation_requested",
        "job": _job_summary(job),
    }


def _check_job_cancelled(job: BridgeJob, reason: str | None = None) -> None:
    if job.cancellation_event.is_set():
        raise BridgeCancellationError(reason or job.cancellation_reason or "cancelled_by_user")


def _call_accepts_keyword(callable_obj: Callable[..., Any], keyword: str) -> bool:
    try:
        sig = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    for parameter in sig.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if (
            parameter.kind
            in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
            and parameter.name == keyword
        ):
            return True
    return False


def _run_agent_turn_with_optional_cancellation(
    agent_session: Any,
    message: str,
    *,
    image_paths: list[str] | None = None,
    ephemeral_user_messages: list[str] | None = None,
    cancellation_token: BridgeCancellationToken,
) -> Any:
    run_turn = agent_session.run_turn
    kwargs: dict[str, Any] = {}
    if image_paths is not None:
        kwargs["image_paths"] = image_paths
    if ephemeral_user_messages:
        if not _call_accepts_keyword(run_turn, "ephemeral_user_messages"):
            raise RuntimeError(
                "This session runtime does not support IDE context; the prompt was not run."
            )
        kwargs["ephemeral_user_messages"] = ephemeral_user_messages
    if _call_accepts_keyword(run_turn, "cancellation_token"):
        kwargs["cancellation_token"] = cancellation_token
    return run_turn(message, **kwargs)


def _list_of_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list | tuple):
        return [str(item) for item in value if str(item)]
    return []


def _optional_task_ids(value: Any, *, request_id: RequestId) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProtocolError("invalid_field", "task_ids must be an array.", request_id=request_id)
    task_ids: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ProtocolError(
                "invalid_field",
                "task_ids must contain non-empty strings.",
                request_id=request_id,
            )
        task_ids.append(item.strip())
    return task_ids


def _forge_execute_mode_param(params: dict[str, Any], *, request_id: RequestId) -> str:
    mode = str(params.get("mode") or "review").strip().lower()
    if mode not in {"review", "auto"}:
        raise ProtocolError(
            "invalid_mode",
            "forge.execute mode must be review or auto.",
            request_id=request_id,
        )
    return mode


def _forge_execute_preview_mode_param(params: dict[str, Any], *, request_id: RequestId) -> str:
    mode = str(params.get("mode") or "review").strip().lower()
    if mode not in SUPPORTED_MODES:
        raise ProtocolError(
            "invalid_mode",
            "forge.executePreview mode must be readonly, review, or auto.",
            request_id=request_id,
        )
    return mode


def _forge_execute_policy_params(
    cfg: Any, params: dict[str, Any], *, request_id: RequestId
) -> tuple[int, bool]:
    if "subagents_enabled" in params:
        raise ProtocolError(
            "forge_execute_subagents_unsupported",
            "IDE Forge Execute v1 does not support subagents_enabled. "
            "features.forge.execute.subagents_supported is false.",
            request_id=request_id,
        )
    max_steps = _positive_int_param(
        params,
        "max_steps",
        default=int(getattr(cfg, "max_steps", 1) or 1),
        request_id=request_id,
    )
    no_log = _optional_bool(params, "no_log", default=False, request_id=request_id)
    return max_steps, no_log


def _bounded_int(
    value: Any,
    *,
    default: int,
    upper: int,
    request_id: RequestId,
) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as e:
        raise ProtocolError(
            "invalid_field", "Numeric fields must be integers.", request_id=request_id
        ) from e
    return max(1, min(parsed, upper))


def _optional_int(value: Any, *, request_id: RequestId) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as e:
        raise ProtocolError(
            "invalid_field", "Numeric fields must be integers.", request_id=request_id
        ) from e
    if parsed < 0:
        raise ProtocolError(
            "invalid_field", "Sequence numbers must be non-negative.", request_id=request_id
        )
    return parsed


def _validate_session_id(session_id: str, *, request_id: RequestId) -> None:
    if not session_id or len(session_id) > SESSION_ID_MAX_LENGTH:
        raise ProtocolError(
            "invalid_session_id", "session_id length is invalid.", request_id=request_id
        )
    if SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise ProtocolError(
            "invalid_session_id",
            "session_id contains unsupported characters.",
            request_id=request_id,
        )


def _run_start_session_id(workspace_root: Path, idempotency_key: str) -> str:
    """Return an opaque stable queue scope for a new-run acceptance request."""

    workspace_identity = os.path.normcase(os.fspath(workspace_root.resolve()))
    digest = hashlib.sha256(
        f"alysis-ide-run-start-v1\0{workspace_identity}\0{idempotency_key}".encode()
    ).hexdigest()
    return f"ide-start-{digest[:48]}"


def _host_capabilities_param(
    params: dict[str, Any],
    *,
    request_id: RequestId,
) -> frozenset[str]:
    raw = params.get("host_capabilities")
    if raw is None:
        return frozenset()
    if not isinstance(raw, dict):
        raise ProtocolError(
            "invalid_host_capabilities",
            "host_capabilities must be an object.",
            request_id=request_id,
        )
    if type(raw.get("approval_protocol", 0)) is not int or raw.get("approval_protocol", 0) not in (
        0,
        1,
    ):
        raise ProtocolError(
            "invalid_host_capabilities",
            "Unsupported host approval protocol.",
            request_id=request_id,
        )
    if set(raw) - {"protocol_version", "actions", "approval_protocol"}:
        raise ProtocolError(
            "invalid_host_capabilities",
            "host_capabilities contains unsupported fields.",
            request_id=request_id,
        )
    if raw.get("protocol_version") != HOST_ACTION_PROTOCOL_VERSION:
        raise ProtocolError(
            "unsupported_host_action_protocol",
            f"host_capabilities.protocol_version must be {HOST_ACTION_PROTOCOL_VERSION}.",
            request_id=request_id,
        )
    actions = raw.get("actions")
    if not isinstance(actions, list) or len(actions) > len(HOST_ACTIONS):
        raise ProtocolError(
            "invalid_host_capabilities",
            "host_capabilities.actions must be a bounded array.",
            request_id=request_id,
        )
    if any(not isinstance(action, str) or not action.strip() for action in actions):
        raise ProtocolError(
            "invalid_host_capabilities",
            "host_capabilities.actions must contain non-empty strings.",
            request_id=request_id,
        )
    normalized = [str(action).strip() for action in actions]
    if len(normalized) != len(set(normalized)):
        raise ProtocolError(
            "invalid_host_capabilities",
            "host_capabilities.actions must not contain duplicates.",
            request_id=request_id,
        )
    unsupported = sorted(set(normalized) - HOST_ACTION_SET)
    if unsupported:
        raise ProtocolError(
            "unsupported_host_action",
            "host_capabilities.actions includes an unsupported action.",
            request_id=request_id,
        )
    return frozenset(normalized)


def _host_capability_fingerprint(
    *,
    root: Path,
    workspace_trusted: bool,
    actions: frozenset[str],
    host_mcp: dict[str, Any] | None = None,
    google_mcp: list[dict[str, Any]] | None = None,
) -> str:
    payload = {
        "protocol_version": HOST_ACTION_PROTOCOL_VERSION,
        "workspace_root": os.path.normcase(os.fspath(root.resolve())),
        "workspace_trusted": bool(workspace_trusted),
        "actions": sorted(actions),
    }
    if host_mcp is not None:
        payload["host_mcp"] = host_mcp
    if google_mcp:
        payload["google_mcp"] = google_mcp
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _host_actions_session_payload(
    session: BridgeSession,
    *,
    request_timeout_seconds: float,
) -> dict[str, Any]:
    return {
        "protocol_version": HOST_ACTION_PROTOCOL_VERSION,
        "actions": sorted(session.host_actions),
        "workspace_trusted": session.workspace_trusted,
        "workspace_fence": session.workspace_fence,
        "capability_fingerprint": session.host_capability_fingerprint,
        "request_event": "host_action_requested",
        "cancellation_event": "host_action_cancelled",
        "session_closed_event": "session_closed",
        "response_method": "host.action.respond",
        "request_timeout_seconds": request_timeout_seconds,
        "approval_protocol": session.host_approval_protocol,
        "max_argument_bytes": HOST_ACTION_MAX_ARGUMENT_BYTES,
        "max_result_bytes": HOST_ACTION_MAX_RESULT_BYTES,
    }


def _validate_host_action_id(host_action_id: str, *, request_id: RequestId) -> None:
    if not re.fullmatch(r"ha_[a-f0-9]{32}", host_action_id):
        raise ProtocolError(
            "invalid_host_action_id",
            "host_action_id is invalid.",
            request_id=request_id,
        )


def _validate_workspace_fence(workspace_fence: str, *, request_id: RequestId) -> None:
    if not re.fullmatch(r"wf_[a-f0-9]{32}", workspace_fence):
        raise ProtocolError(
            "invalid_host_action_workspace_fence",
            "workspace_fence is invalid.",
            request_id=request_id,
        )


def _validate_host_capability_fingerprint(
    capability_fingerprint: str,
    *,
    request_id: RequestId,
) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", capability_fingerprint):
        raise ProtocolError(
            "invalid_host_action_capability_fingerprint",
            "capability_fingerprint is invalid.",
            request_id=request_id,
        )


def _bounded_host_action_reason(reason: Any) -> str:
    return str(redact_secrets(str(reason or "cancelled_by_user")))[:256]


def _run_start_request_fingerprint(
    *,
    workspace_root: Path,
    params: dict[str, Any],
) -> str:
    """Hash the semantic initial-run payload without storing its contents."""

    normalized = dict(params)
    normalized.pop("workspace", None)
    normalized.pop("session_id", None)
    normalized.pop("idempotency_key", None)
    instruction = normalized.pop("instruction", None)
    if "message" not in normalized and instruction is not None:
        normalized["message"] = instruction
    payload = {
        "workspace": os.path.normcase(os.fspath(workspace_root.resolve())),
        "request": normalized,
    }
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        # Protocol requests are JSON, so reaching this branch indicates an
        # internal caller supplied a non-JSON value. Keep the failure generic.
        raise ProtocolError("invalid_request", "run.start payload is not valid JSON.") from None
    return hashlib.sha256(encoded).hexdigest()


def _job_summary(job: BridgeJob) -> dict[str, Any]:
    error = str(redact_secrets(job.error)) if job.error else None
    return {
        "job_id": job.job_id,
        "session_id": job.session_id,
        "kind": job.kind,
        "status": job.status,
        "state": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "updated_at": job.updated_at,
        "completed_at": job.completed_at,
        "cancellable": bool(job.cancellable and job.status in {"queued", "running"}),
        "cancellation_reason": job.cancellation_reason,
        "cancellation_requested_at": job.cancellation_requested_at,
        "last_error": error,
        "exit_code": job.exit_code,
        "error": error,
        "plan_id": job.plan_id,
        "result": redact_secrets(job.result) if job.result is not None else None,
        "event_count": job.event_count,
        "dropped_event_count": job.dropped_event_count,
    }


def _forge_plan_request_fingerprint_payload(params: dict[str, Any]) -> dict[str, Any]:
    """Return semantic request fields for hashing, never durable serialization."""

    excluded = {
        "idempotency_key",
        "session_id",
        "workspace",
        "workspace_trusted",
        "confirm",
        "confirmation",
    }
    return {str(key): value for key, value in params.items() if str(key) not in excluded}


def _durable_forge_protocol_status(state: ForgeRequestState) -> str:
    if state is ForgeRequestState.INDETERMINATE:
        return "failed"
    return state.value


def _iso_from_epoch(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC).isoformat()


def _durable_forge_job_summary(
    record: ForgeRequestRecord,
    *,
    memory_job: BridgeJob | None = None,
) -> dict[str, Any]:
    status = _durable_forge_protocol_status(record.state)
    if (
        memory_job is not None
        and memory_job.status == "cancellation_requested"
        and record.state in {ForgeRequestState.QUEUED, ForgeRequestState.RUNNING}
    ):
        status = "cancellation_requested"
    error = (
        "Forge Plan execution is indeterminate and will not be retried automatically."
        if record.state is ForgeRequestState.INDETERMINATE
        else str(redact_secrets(memory_job.error))
        if record.state is ForgeRequestState.FAILED and memory_job is not None and memory_job.error
        else "Forge Plan job failed."
        if record.state is ForgeRequestState.FAILED
        else None
    )
    return {
        "job_id": record.job_id,
        "session_id": record.session_id,
        "kind": "forge_plan",
        "status": status,
        "state": status,
        "created_at": _iso_from_epoch(record.created_at),
        "started_at": _iso_from_epoch(record.started_at),
        "updated_at": _iso_from_epoch(record.updated_at),
        "completed_at": _iso_from_epoch(record.terminal_at),
        "cancellable": bool(
            record.state in {ForgeRequestState.QUEUED, ForgeRequestState.RUNNING}
            and memory_job is not None
            and memory_job.cancellable
        ),
        "cancellation_reason": (
            record.error_code if record.state is ForgeRequestState.CANCELLED else None
        ),
        "cancellation_requested_at": (
            memory_job.cancellation_requested_at if memory_job is not None else None
        ),
        "last_error": error,
        "exit_code": (
            1
            if record.state in {ForgeRequestState.FAILED, ForgeRequestState.INDETERMINATE}
            else 130
            if record.state is ForgeRequestState.CANCELLED
            else None
        ),
        "error": error,
        "error_code": record.error_code,
        "plan_id": record.plan_id,
        "result": None,
        "event_count": memory_job.event_count if memory_job is not None else 0,
        "dropped_event_count": (memory_job.dropped_event_count if memory_job is not None else 0),
        "durable": True,
        "attempts": record.attempts,
    }


def _session_summary(session: BridgeSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "workspace_root": os.fspath(session.root),
        "mode": session.mode,
        "closed": session.closed,
        "active_job": _job_summary(session.active_job) if session.active_job is not None else None,
        "last_job": _job_summary(session.last_job) if session.last_job is not None else None,
    }


def _json_clone(payload: dict[str, Any]) -> dict[str, Any]:
    import json

    return json.loads(json.dumps(payload, ensure_ascii=True))


def _private_protocol_stream(stream: TextIO, mode: str) -> TextIO:
    """Duplicate a standard stream onto a descriptor no child process can inherit."""
    descriptor = os.dup(stream.fileno())
    os.set_inheritable(descriptor, False)
    return io.TextIOWrapper(
        open(descriptor, mode),
        encoding="utf-8",
        errors="strict",
        # Match CPython's own stdio wrappers: universal newlines on Windows, "\n" elsewhere.
        newline=None if os.name == "nt" else "\n",
    )


def claim_protocol_streams() -> tuple[TextIO, TextIO]:
    """Make the JSONL transport private to the bridge.

    Child processes (git, search tools, hooks) inherit descriptors 0 and 1 unless the
    caller overrides them. Pointing at the protocol pipes, a child could consume a
    request meant for the bridge or write into the response stream. On Windows the
    pipes are synchronous, so the system serialises every operation on them behind the
    bridge's pending read: a child that merely inspects its inherited stdin at start-up
    blocks until the next request arrives, and a turn waiting for that child never
    finishes. The bridge keeps private duplicates, and descriptors 0 and 1 (with the
    Windows standard handles the C runtime keeps in step with them) move to the null
    device.
    """
    sys.stdout.flush()
    protocol_stdin = _private_protocol_stream(sys.stdin, "rb")
    protocol_stdout = _private_protocol_stream(sys.stdout, "wb")
    null_descriptor = os.open(os.devnull, os.O_RDWR)
    try:
        os.dup2(null_descriptor, sys.stdin.fileno())
        os.dup2(null_descriptor, sys.stdout.fileno())
    finally:
        os.close(null_descriptor)
    return protocol_stdin, protocol_stdout


def run_stdio_bridge() -> int:
    # stdout is the protocol transport. Agent/provider integrations are allowed to
    # print diagnostics, but a single stray print would otherwise corrupt the JSONL
    # stream and strand every pending extension request. Capture the protocol streams
    # first, then route incidental process-wide stdout to stderr for the lifetime of
    # this dedicated bridge process. StdioBridge reads and writes through the captured
    # handles, which no child process can inherit.
    protocol_stdin, protocol_stdout = claim_protocol_streams()
    bridge = StdioBridge(stdin=protocol_stdin, stdout=protocol_stdout)
    # Backstop for a hard-killed extension host: stdin EOF is the normal exit path,
    # this guarantees the interpreter cannot outlive its parent when that path stalls.
    start_parent_watchdog(
        grace_seconds=DEFAULT_SHUTDOWN_TIMEOUT_SECONDS + PARENT_EXIT_GRACE_SECONDS,
        on_parent_exit=lambda pid: _close_orphaned_bridge(bridge, pid),
    )
    with redirect_stdout(sys.stderr):
        return bridge.run()


def _close_orphaned_bridge(bridge: StdioBridge, parent_pid: int) -> None:
    print(
        f"IDE extension host (pid {parent_pid}) exited; shutting the bridge down",
        file=sys.stderr,
    )
    bridge.close()
