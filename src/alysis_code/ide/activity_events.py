from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from ..surface.types import PatchEvent
from .protocol import redact_secrets

ActivityKind: TypeAlias = Literal[
    "read",
    "search",
    "edit",
    "command",
    "test",
    "git",
    "network",
    "browser",
    "diagnostic",
    "subagent",
    "service",
    "plan",
    "other",
]
ActivityStatus: TypeAlias = Literal[
    "queued", "running", "succeeded", "failed", "cancelled", "blocked", "completed_unverified"
]

MAX_ACTIVITY_TEXT_CHARS = 500
MAX_ACTIVITY_METADATA_ITEMS = 32
MAX_PATCH_BYTES = 512 * 1024
ACTIVITY_EVENT_TYPE = "activity_update"
ACTIVITY_KINDS = (
    "read",
    "search",
    "edit",
    "command",
    "test",
    "git",
    "network",
    "browser",
    "diagnostic",
    "subagent",
    "service",
    "plan",
    "other",
)
ACTIVITY_STATUSES = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "blocked",
    "completed_unverified",
)

_SAFE_METADATA_KEYS = frozenset(
    {
        "artifact_id",
        "attempt",
        "cached",
        "exit_code",
        "file_count",
        "line_count",
        "output_bytes",
        "result_count",
        "retry_count",
        "source",
        "step",
        "truncated",
        "worker_id",
        "task_outcome",
        "verified_success",
    }
)
_INTERNAL_NAME_PATTERN = re.compile(r"(?:^|[._:/-])(?:rs|internal)(?:[._:/-]|$)", re.I)


@dataclass(frozen=True, slots=True)
class DiffStats:
    files: int = 0
    additions: int = 0
    deletions: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "files": max(0, int(self.files)),
            "additions": max(0, int(self.additions)),
            "deletions": max(0, int(self.deletions)),
        }


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    activity_id: str
    kind: ActivityKind
    operation: str
    display_title: str
    status: ActivityStatus
    target: str | None = None
    summary: str | None = None
    duration_ms: int | None = None
    diff: DiffStats | None = None
    files: tuple[str, ...] = ()
    patch: str | None = None
    patch_truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "activity_id": _safe_text(self.activity_id, max_chars=128),
            "kind": self.kind,
            "operation": _safe_operation(self.operation),
            "display_title": _safe_title(self.display_title),
            "status": self.status,
            "target": _safe_optional_text(self.target),
            "summary": _safe_optional_text(self.summary),
            "duration_ms": (
                max(0, int(self.duration_ms)) if self.duration_ms is not None else None
            ),
            "diff": self.diff.to_dict() if self.diff is not None else None,
            "files": [_safe_text(path, max_chars=4096) for path in self.files[:200]],
            "patch": self.patch,
            "patch_truncated": bool(self.patch_truncated),
            "metadata": sanitize_activity_metadata(self.metadata),
        }
        return redact_secrets(payload)


@dataclass(frozen=True, slots=True)
class ToolSemantic:
    kind: ActivityKind
    operation: str
    display_title: str
    target_fields: tuple[str, ...] = ()


_TOOL_SEMANTICS: dict[str, ToolSemantic] = {
    "task_artifacts": ToolSemantic("plan", "check_requested_files", "Check requested files"),
    "open_file_preview": ToolSemantic("read", "read_file", "Read file", ("path",)),
    "fs_read": ToolSemantic("read", "read_file", "Read file", ("path", "file")),
    "fs_read_lines": ToolSemantic("read", "read_file_range", "Read file lines", ("path",)),
    "fs_list": ToolSemantic("read", "list_directory", "List directory", ("path",)),
    "repo_map": ToolSemantic("search", "map_repository", "Map repository", ("path",)),
    "search_rg": ToolSemantic("search", "search_workspace", "Search workspace", ("query",)),
    "symbol_search": ToolSemantic("search", "search_symbols", "Search symbols", ("query",)),
    "history_search": ToolSemantic("search", "search_history", "Search history", ("query",)),
    "fs_edit": ToolSemantic("edit", "edit_file", "Edit file", ("path",)),
    "fs_write": ToolSemantic("edit", "write_file", "Write file", ("path",)),
    "fs_mkdir": ToolSemantic("edit", "create_directory", "Create directory", ("path",)),
    "fs_move": ToolSemantic("edit", "move_file", "Move file", ("src", "source", "path")),
    "fs_copy": ToolSemantic("edit", "copy_file", "Copy file", ("src", "source", "path")),
    "fs_delete": ToolSemantic("edit", "delete_file", "Delete file", ("path",)),
    "git_apply_patch": ToolSemantic("edit", "apply_patch", "Apply patch", ("path",)),
    "shell_run": ToolSemantic("command", "run_command", "Run command", ("command",)),
    "shell_background": ToolSemantic(
        "command", "start_background_command", "Start background command", ("command",)
    ),
    "shell_output": ToolSemantic("command", "read_command_output", "Read command output"),
    "shell_wait": ToolSemantic("command", "wait_for_command", "Wait for command"),
    "shell_kill": ToolSemantic("command", "stop_command", "Stop command"),
    "shell_list": ToolSemantic("command", "list_commands", "List running commands"),
    "desktop_terminal_list": ToolSemantic("command", "list_terminals", "List command windows"),
    "desktop_terminal_open": ToolSemantic("command", "open_terminal", "Open command window"),
    "desktop_terminal_read": ToolSemantic(
        "command", "read_terminal", "Read command window", ("terminal_id",)
    ),
    "desktop_terminal_write": ToolSemantic(
        "command", "write_terminal", "Send terminal input", ("terminal_id",)
    ),
    "desktop_terminal_close": ToolSemantic(
        "command", "close_terminal", "Close command window", ("terminal_id",)
    ),
    "session_set_workdir": ToolSemantic(
        "plan", "select_working_folder", "Select working folder", ("path",)
    ),
    "verify_run": ToolSemantic("test", "verify_changes", "Verify changes", ("command",)),
    "test_discover": ToolSemantic("test", "discover_tests", "Discover tests", ("path",)),
    "git_status": ToolSemantic("git", "git_status", "Inspect Git status"),
    "git_diff": ToolSemantic("git", "git_diff", "Inspect Git changes", ("path",)),
    "git_history": ToolSemantic("git", "git_history", "Inspect Git history", ("path",)),
    "web_fetch": ToolSemantic("network", "fetch_web_page", "Fetch web page", ("url",)),
    "web_search": ToolSemantic("network", "search_web", "Search the web", ("query",)),
    "image_generate": ToolSemantic("network", "generate_image", "Generate image", ("prompt",)),
    "workspace_preview_start": ToolSemantic(
        "browser", "preview_workspace", "Start workspace preview", ("path",)
    ),
    "shell_service_start": ToolSemantic(
        "service", "start_service", "Start development service", ("command",)
    ),
    "shell_service_status": ToolSemantic(
        "service", "inspect_service", "Inspect development service"
    ),
    "shell_service_stop": ToolSemantic("service", "stop_service", "Stop development service"),
    "subagent_run": ToolSemantic("subagent", "delegate_task", "Delegate task", ("task",)),
    "skill_read": ToolSemantic("read", "read_skill", "Read skill guidance", ("name", "path")),
    "document_operation": ToolSemantic(
        "other", "document_operation", "Work with document", ("output", "source")
    ),
    "artifact_check": ToolSemantic("test", "check_artifact", "Check file", ("path",)),
    "asset_view": ToolSemantic("read", "view_asset", "Inspect image or video", ("path",)),
    "tool_search": ToolSemantic("search", "discover_tools", "Search connected tools", ("query",)),
}

# These are the document tool's declared API actions, not guesses from filenames
# or model prose. They also keep read-only operations out of the edit category.
_DOCUMENT_ACTION_SEMANTICS = {
    "describe": ToolSemantic(
        "read", "describe_document_tools", "Inspect document tools", ("format",)
    ),
    "inspect": ToolSemantic("read", "inspect_document", "Inspect document", ("source",)),
    "create": ToolSemantic("edit", "create_document", "Create document", ("output",)),
    "edit": ToolSemantic("edit", "edit_document", "Edit document", ("output", "source")),
    "recalculate": ToolSemantic(
        "edit", "recalculate_spreadsheet", "Recalculate spreadsheet", ("output", "source")
    ),
}

_ALIASES = {
    "read": "fs_read",
    "read_file": "fs_read",
    "rs.read": "fs_read",
    "rs_read": "fs_read",
    "write_file": "fs_write",
    "edit_file": "fs_edit",
    "apply_patch": "git_apply_patch",
    "bash": "shell_run",
    "shell": "shell_run",
    "grep": "search_rg",
    "ripgrep": "search_rg",
}


def semantic_tool_name(name: str) -> str:
    """Return a stable public operation name, never a private runtime identifier."""

    normalized = str(name or "").strip().casefold().replace("-", "_")
    normalized = _ALIASES.get(normalized, normalized)
    if normalized in _TOOL_SEMANTICS:
        return normalized
    if normalized.startswith("mcp__") or normalized.startswith("mcp."):
        return "external_tool"
    if _INTERNAL_NAME_PATTERN.search(normalized):
        return "runtime_tool"
    safe = re.sub(r"[^a-z0-9_]+", "_", normalized).strip("_")
    if not safe or safe.startswith(("rs_", "internal_")):
        return "runtime_tool"
    return safe[:80]


def activity_capabilities() -> dict[str, Any]:
    return {
        "event_type": ACTIVITY_EVENT_TYPE,
        "kinds": list(ACTIVITY_KINDS),
        "statuses": list(ACTIVITY_STATUSES),
        "semantic_tool_identity": True,
        "safe_metadata": True,
        "patch_payload": True,
        "max_patch_bytes": MAX_PATCH_BYTES,
        "diff_stats": ["files", "additions", "deletions"],
        "legacy_tool_events_preserved": True,
    }


def tool_activity(
    *,
    call_id: str,
    name: str,
    arguments: Mapping[str, Any] | None = None,
    status: str = "running",
    summary: str | None = None,
    duration_ms: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ActivityEvent:
    public_name = semantic_tool_name(name)
    semantic = _TOOL_SEMANTICS.get(public_name) or _infer_semantic(public_name)
    safe_status = _normalize_status(status)
    safe_arguments = redact_secrets(dict(arguments or {}))
    if public_name == "document_operation":
        action = safe_arguments.get("action")
        if isinstance(action, str):
            semantic = _DOCUMENT_ACTION_SEMANTICS.get(action, semantic)
    target = _target_from_arguments(safe_arguments, semantic.target_fields)
    return ActivityEvent(
        activity_id=call_id,
        kind=semantic.kind,
        operation=semantic.operation,
        display_title=semantic.display_title,
        status=safe_status,
        target=target,
        summary=summary,
        duration_ms=duration_ms,
        metadata=dict(metadata or {}),
    )


def patch_activity(event: PatchEvent, *, activity_id: str | None = None) -> ActivityEvent:
    full_safe_patch = str(redact_secrets(event.diff))
    safe_patch, truncated = _bounded_patch(full_safe_patch)
    truncated = truncated or event.diff_truncated
    stable_activity_id = activity_id or (
        "patch-" + hashlib.sha256(full_safe_patch.encode("utf-8")).hexdigest()[:16]
    )
    safe_files = tuple(_safe_text(path, max_chars=4096) for path in event.files[:200])
    stats = diff_stats(full_safe_patch, file_count=len(event.files))
    summary = str(redact_secrets(event.summary or "Workspace changes prepared"))
    return ActivityEvent(
        activity_id=stable_activity_id,
        kind="edit",
        operation="apply_workspace_patch" if event.applied else "prepare_workspace_patch",
        display_title="Applied workspace changes"
        if event.applied
        else "Prepared workspace changes",
        status="succeeded",
        target=_files_target(safe_files),
        summary=summary,
        diff=stats,
        files=safe_files,
        patch=safe_patch,
        patch_truncated=truncated,
        metadata={"file_count": len(event.files), "truncated": truncated},
    )


def diff_stats(diff: str, *, file_count: int | None = None) -> DiffStats:
    additions = 0
    deletions = 0
    discovered_files: set[str] = set()
    for line in str(diff or "").splitlines():
        if line.startswith("diff --git "):
            discovered_files.add(line)
        elif line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    files = file_count if file_count is not None else len(discovered_files)
    return DiffStats(files=max(0, files), additions=additions, deletions=deletions)


def sanitize_activity_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    safe: dict[str, Any] = {}
    for key in sorted(metadata):
        if len(safe) >= MAX_ACTIVITY_METADATA_ITEMS:
            break
        normalized_key = str(key).strip().casefold()
        if normalized_key not in _SAFE_METADATA_KEYS:
            continue
        value = metadata[key]
        if value is None or isinstance(value, bool | int):
            safe[normalized_key] = value
        elif isinstance(value, float):
            if value == value and value not in {float("inf"), float("-inf")}:
                safe[normalized_key] = value
        elif isinstance(value, str):
            safe[normalized_key] = _safe_text(value)
    return redact_secrets(safe)


def _infer_semantic(public_name: str) -> ToolSemantic:
    tokens = set(public_name.split("_"))
    if tokens & {"read", "list", "show", "inspect", "get"}:
        return ToolSemantic("read", "inspect_resource", "Inspect resource", ("path", "uri"))
    if tokens & {"search", "find", "query"}:
        return ToolSemantic("search", "search_resources", "Search resources", ("query",))
    if tokens & {"edit", "write", "create", "delete", "move", "copy", "patch"}:
        return ToolSemantic("edit", "change_workspace", "Change workspace", ("path",))
    if tokens & {"test", "verify", "check", "lint"}:
        return ToolSemantic("test", "verify_workspace", "Verify workspace", ("command",))
    if tokens & {"browser", "navigate", "click", "screenshot"}:
        return ToolSemantic("browser", "use_browser", "Use browser", ("url",))
    if tokens & {"web", "http", "fetch", "download"}:
        return ToolSemantic("network", "use_network", "Use network", ("url", "query"))
    if tokens & {"shell", "command", "exec", "run"}:
        return ToolSemantic("command", "run_command", "Run command", ("command",))
    if public_name == "external_tool":
        return ToolSemantic("other", "use_external_tool", "Use connected tool")
    return ToolSemantic("other", "use_tool", "Use developer tool")


def _normalize_status(status: str) -> ActivityStatus:
    normalized = str(status or "").strip().casefold()
    if normalized in {"queued", "pending"}:
        return "queued"
    if normalized in {"running", "started", "in_progress", "progress"}:
        return "running"
    if normalized == "completed_unverified":
        return "completed_unverified"
    if normalized in {"done", "completed", "complete", "success", "succeeded", "ok"}:
        return "succeeded"
    if normalized in {"cancelled", "canceled", "interrupted"}:
        return "cancelled"
    if normalized in {"blocked", "denied", "rejected"}:
        return "blocked"
    return "failed"


def _target_from_arguments(arguments: Any, fields: tuple[str, ...]) -> str | None:
    if not isinstance(arguments, Mapping):
        return None
    for field_name in fields:
        value = arguments.get(field_name)
        if isinstance(value, str) and value.strip():
            return _safe_text(value)
    return None


def _files_target(files: tuple[str, ...]) -> str | None:
    if not files:
        return None
    if len(files) == 1:
        return files[0]
    return f"{files[0]} and {len(files) - 1} more"


def _safe_title(value: str) -> str:
    title = _safe_text(value, max_chars=120).strip()
    if not title or _INTERNAL_NAME_PATTERN.search(title):
        return "Developer activity"
    return title


def _safe_operation(value: str) -> str:
    operation = re.sub(r"[^a-z0-9_]+", "_", str(value).strip().casefold()).strip("_")
    if not operation or operation.startswith(("rs_", "internal_")):
        return "use_tool"
    return operation[:80]


def _safe_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    return _safe_text(value)


def _safe_text(value: str, *, max_chars: int = MAX_ACTIVITY_TEXT_CHARS) -> str:
    text = str(redact_secrets(str(value)))
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)] + "..."


def _bounded_patch(diff: str) -> tuple[str, bool]:
    encoded = diff.encode("utf-8")
    if len(encoded) <= MAX_PATCH_BYTES:
        return diff, False
    marker = "\n[...patch truncated...]"
    budget = MAX_PATCH_BYTES - len(marker.encode("utf-8"))
    prefix = encoded[: max(0, budget)].decode("utf-8", errors="ignore")
    return prefix + marker, True
