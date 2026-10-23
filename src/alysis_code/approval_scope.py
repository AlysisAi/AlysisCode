from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

SCOPE_EXACT_COMMAND_HASH = "exact_command_hash"
SCOPE_EXACT_FILE_SET = "exact_file_set"
SCOPE_EXACT_VERIFY_COMMAND_SET = "exact_verify_command_set"
SCOPE_EXPLICIT_BACKEND_SAFE_KIND = "explicit_backend_safe_kind"

SUPPORTED_APPROVAL_SCOPES = frozenset(
    {
        SCOPE_EXACT_COMMAND_HASH,
        SCOPE_EXACT_FILE_SET,
        SCOPE_EXACT_VERIFY_COMMAND_SET,
        SCOPE_EXPLICIT_BACKEND_SAFE_KIND,
    }
)

_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_FILE_SCOPE_KINDS = frozenset(
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
_COMMAND_SCOPE_KINDS = frozenset({"shell_run", "shell_background"})
_DESTRUCTIVE_APPROVAL_KINDS = frozenset({"fs_delete"})
_DESTRUCTIVE_COMMAND_PATTERN = re.compile(
    r"(?:^|[;&|]\s*|\bsudo\s+)(?:rm\s+(?:-[^\s]*[rR][^\s]*\s+|--recursive\b)|"
    r"rmdir\b|git\s+(?:reset\s+--hard|clean\s+-[^\s]*f|checkout\s+--)|"
    r"(?:drop\s+(?:database|schema|table)|truncate\s+table)\b)",
    re.IGNORECASE,
)

# ---- directory-scoped session grants --------------------------------------
# An exact-file-set "always" can never cover the next file of a batch (every
# file set hashes differently), so deletes prompted once per file with a
# useless [a]. A directory grant is the explicit, user-chosen widening: allow
# THIS operation for files under THIS directory, for this session. Deliberately
# restricted to fs_delete - review-mode write approvals exist to show each
# change and stay per-file.
_DIR_GRANT_KINDS = frozenset({"fs_delete"})


def dir_grants_enabled() -> bool:
    """Kill switch ``ALYSIS_APPROVAL_DIR_GRANTS`` (default on)."""
    value = os.environ.get("ALYSIS_APPROVAL_DIR_GRANTS", "").strip().lower()
    return value not in {"0", "false", "off", "no"}


def _plain_relative_file_path(text: str) -> bool:
    """A workspace-relative path with no absolute prefix and no dot segments."""
    if not text or text.startswith("/"):
        return False
    return all(segment not in {"", ".", ".."} for segment in text.split("/"))


def approval_dir_grant_candidate(request: Any) -> str | None:
    """Directory a folder-wide session grant would cover for this request.

    Returns the workspace-relative directory (posix, trailing ``/``) when the
    request is a dir-grantable kind whose files all live in one directory that
    is not the workspace root; ``None`` otherwise (including for approvals
    marked ``allow_for_session_disabled`` - sensitive-file prompts never widen).
    """
    if not dir_grants_enabled():
        return None
    kind = str(getattr(request, "kind", "") or "")
    if kind not in _DIR_GRANT_KINDS:
        return None
    metadata = getattr(request, "metadata", None)
    if isinstance(metadata, dict) and metadata.get("allow_for_session_disabled"):
        return None
    files = getattr(request, "files", None) or ()
    normalized = _normalize_files(files if isinstance(files, list | tuple) else ())
    if not normalized:
        return None
    directories: set[str] = set()
    for text in normalized:
        if not _plain_relative_file_path(text):
            return None
        directories.add(text.rsplit("/", 1)[0] if "/" in text else "")
    if len(directories) != 1:
        return None
    directory = next(iter(directories))
    if not directory:
        return None
    return directory + "/"


def request_matches_dir_grants(request: Any, grants: Iterable[tuple[str, str]]) -> bool:
    """Whether every file of ``request`` falls under a stored (kind, dir) grant."""
    if not dir_grants_enabled():
        return False
    kind = str(getattr(request, "kind", "") or "")
    if kind not in _DIR_GRANT_KINDS:
        return False
    metadata = getattr(request, "metadata", None)
    if isinstance(metadata, dict) and metadata.get("allow_for_session_disabled"):
        return False
    files = getattr(request, "files", None) or ()
    normalized = _normalize_files(files if isinstance(files, list | tuple) else ())
    if not normalized:
        return False
    applicable = {directory for grant_kind, directory in grants if grant_kind == kind}
    if not applicable:
        return False
    for text in normalized:
        if not _plain_relative_file_path(text):
            return False
        if not any(text.startswith(directory) for directory in applicable):
            return False
    return True


@dataclass(frozen=True, slots=True)
class ApprovalSessionScope:
    supported: bool
    scope: dict[str, Any] | None
    key: str | None
    warning: str | None


def exact_command_scope(command: str, *, kind: str | None = None) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "type": SCOPE_EXACT_COMMAND_HASH,
        "algorithm": "sha256",
        "command_hash": _sha256_text(command),
    }
    if kind:
        scope["kind"] = str(kind)
    return scope


def exact_file_set_scope(files: Iterable[str], *, operation: str | None = None) -> dict[str, Any]:
    normalized = _normalize_files(files)
    scope: dict[str, Any] = {
        "type": SCOPE_EXACT_FILE_SET,
        "algorithm": "sha256",
        "files_hash": _sha256_json(list(normalized)),
        "file_count": len(normalized),
        "files": list(normalized),
    }
    if operation:
        scope["operation"] = str(operation)
    return scope


def exact_verify_command_set_scope(commands: Iterable[str]) -> dict[str, Any]:
    normalized = tuple(str(command) for command in commands)
    return {
        "type": SCOPE_EXACT_VERIFY_COMMAND_SET,
        "algorithm": "sha256",
        "commands_hash": _sha256_json(list(normalized)),
        "command_count": len(normalized),
    }


def explicit_backend_safe_kind_scope(kind: str) -> dict[str, Any]:
    return {
        "type": SCOPE_EXPLICIT_BACKEND_SAFE_KIND,
        "safe_kind": str(kind),
        "backend_owned": True,
    }


def approval_session_scope_for_request(request: Any) -> ApprovalSessionScope:
    kind = str(getattr(request, "kind", "") or "approval")
    raw_scope = getattr(request, "allow_for_session_scope", None)
    metadata = getattr(request, "metadata", None)
    if raw_scope is None and isinstance(metadata, dict):
        candidate = metadata.get("allow_for_session_scope")
        if isinstance(candidate, dict):
            raw_scope = candidate
    if not isinstance(raw_scope, dict):
        return _unsupported(
            "Allow for session is unavailable because this approval did not include an exact safe scope."
        )

    scope_type = str(raw_scope.get("type") or raw_scope.get("scope_type") or "").strip()
    if scope_type not in SUPPORTED_APPROVAL_SCOPES:
        return _unsupported(
            "Allow for session is unavailable because the approval scope is unsupported."
        )

    if _is_command_like_kind(kind):
        if scope_type != SCOPE_EXACT_COMMAND_HASH:
            return _unsupported(
                "Allow for session is unavailable because command approvals require an exact command hash scope."
            )
    elif kind == "verify_run":
        if scope_type != SCOPE_EXACT_VERIFY_COMMAND_SET:
            return _unsupported(
                "Allow for session is unavailable because verification approvals require an exact verification command set scope."
            )
    elif kind in _FILE_SCOPE_KINDS:
        if scope_type != SCOPE_EXACT_FILE_SET:
            return _unsupported(
                "Allow for session is unavailable because file approvals require an exact file set scope."
            )
    elif scope_type != SCOPE_EXPLICIT_BACKEND_SAFE_KIND:
        return _unsupported(
            "Allow for session is unavailable because this approval kind is not backend-marked as session safe."
        )

    if scope_type == SCOPE_EXACT_COMMAND_HASH:
        return _command_hash_scope(kind, raw_scope, getattr(request, "command", None))
    if scope_type == SCOPE_EXACT_FILE_SET:
        return _file_set_scope(kind, raw_scope, getattr(request, "files", ()))
    if scope_type == SCOPE_EXACT_VERIFY_COMMAND_SET:
        return _verify_command_set_scope(kind, raw_scope)
    return _explicit_backend_safe_kind_scope(kind, raw_scope)


def _command_hash_scope(
    kind: str,
    raw_scope: dict[str, Any],
    command: Any,
) -> ApprovalSessionScope:
    command_hash = str(raw_scope.get("command_hash") or "").strip().lower()
    if _SHA256_PATTERN.fullmatch(command_hash) is None:
        return _unsupported(
            "Allow for session is unavailable because the command scope is malformed."
        )
    if command is not None and _sha256_text(str(command)) != command_hash:
        return _unsupported(
            "Allow for session is unavailable because the command scope does not match the approval request."
        )
    scope = {
        "type": SCOPE_EXACT_COMMAND_HASH,
        "algorithm": "sha256",
        "kind": kind,
        "command_hash": command_hash,
    }
    return _supported(scope)


def _file_set_scope(
    kind: str,
    raw_scope: dict[str, Any],
    files: Any,
) -> ApprovalSessionScope:
    files_hash = str(raw_scope.get("files_hash") or "").strip().lower()
    normalized_files = _normalize_files(files if isinstance(files, list | tuple) else ())
    if _SHA256_PATTERN.fullmatch(files_hash) is None:
        return _unsupported("Allow for session is unavailable because the file scope is malformed.")
    if not normalized_files:
        return _unsupported(
            "Allow for session is unavailable because file approvals require at least one scoped file."
        )
    if _sha256_json(list(normalized_files)) != files_hash:
        return _unsupported(
            "Allow for session is unavailable because the file scope does not match the approval request."
        )
    scope = {
        "type": SCOPE_EXACT_FILE_SET,
        "algorithm": "sha256",
        "kind": kind,
        "operation": str(raw_scope.get("operation") or kind),
        "files_hash": files_hash,
        "file_count": len(normalized_files),
        "files": list(normalized_files),
    }
    return _supported(scope)


def _verify_command_set_scope(kind: str, raw_scope: dict[str, Any]) -> ApprovalSessionScope:
    commands_hash = str(raw_scope.get("commands_hash") or "").strip().lower()
    if _SHA256_PATTERN.fullmatch(commands_hash) is None:
        return _unsupported(
            "Allow for session is unavailable because the verification scope is malformed."
        )
    command_count = _positive_int(raw_scope.get("command_count"))
    if command_count is None:
        return _unsupported(
            "Allow for session is unavailable because the verification scope is missing its command count."
        )
    scope = {
        "type": SCOPE_EXACT_VERIFY_COMMAND_SET,
        "algorithm": "sha256",
        "kind": kind,
        "commands_hash": commands_hash,
        "command_count": command_count,
    }
    return _supported(scope)


def _explicit_backend_safe_kind_scope(
    kind: str,
    raw_scope: dict[str, Any],
) -> ApprovalSessionScope:
    safe_kind = str(raw_scope.get("safe_kind") or "").strip()
    if safe_kind != kind or raw_scope.get("backend_owned") is not True:
        return _unsupported(
            "Allow for session is unavailable because the backend safe-kind scope is malformed."
        )
    scope = {
        "type": SCOPE_EXPLICIT_BACKEND_SAFE_KIND,
        "kind": kind,
        "safe_kind": safe_kind,
    }
    return _supported(scope)


def _supported(scope: dict[str, Any]) -> ApprovalSessionScope:
    key = json.dumps(scope, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return ApprovalSessionScope(supported=True, scope=scope, key=key, warning=None)


def _unsupported(warning: str) -> ApprovalSessionScope:
    return ApprovalSessionScope(supported=False, scope=None, key=None, warning=warning)


def _normalize_files(files: Iterable[Any]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for file in files:
        text = str(file).strip().replace("\\", "/")
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return tuple(sorted(normalized))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _is_command_like_kind(kind: str) -> bool:
    return (
        kind in _COMMAND_SCOPE_KINDS
        or kind.startswith("custom_tool_run:")
        or kind.startswith("mcp_")
        or kind.startswith("mcp_tool_run:")
    )


def approval_request_is_destructive(request: Any) -> bool:
    kind = str(getattr(request, "kind", "") or "approval").strip().casefold()
    if kind in _DESTRUCTIVE_APPROVAL_KINDS:
        return True
    metadata = getattr(request, "metadata", None)
    if isinstance(metadata, dict) and metadata.get("destructive") is True:
        return True
    command = getattr(request, "command", None)
    return bool(command and _DESTRUCTIVE_COMMAND_PATTERN.search(str(command)))
