from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..approval_scope import approval_request_is_destructive
from .protocol import redact_secrets

APPROVAL_BINDING_CONTRACT = "alysis.approval-binding"
APPROVAL_BINDING_VERSION = 1
APPROVAL_ACTION_SCHEMA = "alysis.approval-action"
APPROVAL_ACTION_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ApprovalBinding:
    action: dict[str, Any]
    action_fingerprint: str
    project_fingerprint: str
    runtime_fingerprint: str
    policy_fingerprint: str
    operation_id: str | None
    job_id: str | None
    tool_call_id: str | None
    destructive: bool

    def renderer_payload(self) -> dict[str, Any]:
        return {
            "contract": APPROVAL_BINDING_CONTRACT,
            "version": APPROVAL_BINDING_VERSION,
            "action": _json_clone(self.action),
            "action_fingerprint": self.action_fingerprint,
            "project_fingerprint": self.project_fingerprint,
            "runtime_fingerprint": self.runtime_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "operation_id": self.operation_id,
            "job_id": self.job_id,
            "tool_call_id": self.tool_call_id,
            "destructive": self.destructive,
        }


def build_approval_binding(
    request: Any,
    *,
    project_root: str | Path,
    runtime_fingerprint: str,
    mode: str,
    operation_id: str | None = None,
    job_id: str | None = None,
    tool_call_id: str | None = None,
    allow_for_session_supported: bool = False,
) -> ApprovalBinding:
    canonical_project_root = _canonical_path(project_root, base=None)
    action = normalize_approval_action(request, project_root=canonical_project_root)
    destructive = bool(action["destructive"])
    project_fingerprint = sha256_canonical_json({"project_root": canonical_project_root})
    policy_fingerprint = approval_policy_fingerprint(
        mode=mode,
        kind=str(action["kind"]),
        destructive=destructive,
        allow_for_session_supported=allow_for_session_supported,
    )
    binding = {
        "project_fingerprint": project_fingerprint,
        "runtime_fingerprint": _normalized_identifier(runtime_fingerprint),
        "policy_fingerprint": policy_fingerprint,
        "operation_id": _optional_identifier(operation_id),
        "job_id": _optional_identifier(job_id),
        "tool_call_id": _optional_identifier(tool_call_id),
    }
    action_fingerprint = sha256_canonical_json({"action": action, "binding": binding})
    return ApprovalBinding(
        action=action,
        action_fingerprint=action_fingerprint,
        project_fingerprint=project_fingerprint,
        runtime_fingerprint=binding["runtime_fingerprint"],
        policy_fingerprint=policy_fingerprint,
        operation_id=binding["operation_id"],
        job_id=binding["job_id"],
        tool_call_id=binding["tool_call_id"],
        destructive=destructive,
    )


def normalize_approval_action(
    request: Any,
    *,
    project_root: str | Path,
) -> dict[str, Any]:
    root = _canonical_path(project_root, base=None)
    kind = _normalized_kind(getattr(request, "kind", None))
    command_raw = getattr(request, "command", None)
    command = _normalized_text(command_raw) if command_raw is not None else None
    metadata = getattr(request, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    cwd_raw = metadata.get("cwd") or metadata.get("working_directory") or root
    cwd = _canonical_path(cwd_raw, base=root)
    ordered_files = [
        _canonical_path(value, base=root)
        for value in getattr(request, "files", ())
        if str(value).strip()
    ]
    ordered_files = list(dict.fromkeys(ordered_files))
    files = sorted(ordered_files)
    destructive = approval_request_is_destructive(request)
    action: dict[str, Any] = {
        "schema": APPROVAL_ACTION_SCHEMA,
        "schema_version": APPROVAL_ACTION_SCHEMA_VERSION,
        "kind": kind,
        "access": _access_for_kind(kind),
        "destructive": destructive,
        "project_root": root,
        "cwd": cwd,
        "command": command,
        "files": files,
        "file_arguments": ordered_files,
        "tool_identity": _tool_identity(metadata),
    }
    from ..agent.artifact_output_observation import FileMetadataApproval

    if isinstance(request, FileMetadataApproval):
        action["access"] = "read"
        action["risk_level"] = "unknown"
    if kind == "sandbox_access":
        from ..agent.scoped_access import SandboxAccessApproval

        # A capability request has no intrinsic risk score until assessed.
        # Only the internal admission type supplies capability access metadata.
        action["risk_level"] = "unknown"
        if isinstance(request, SandboxAccessApproval) and metadata.get("access") in {
            "read",
            "write",
            "network",
            "control",
        }:
            action["access"] = metadata["access"]
    assessment = metadata.get("approval_review")
    if isinstance(assessment, dict):
        if assessment.get("risk") in {"low", "medium", "high", "critical", "unknown"}:
            action["risk_level"] = assessment["risk"]
        if assessment.get("intent_level") in {"low", "medium", "high", "unknown"}:
            action["intent_level"] = assessment["intent_level"]
    if isinstance(metadata.get("host_action"), str):
        # Main's negotiated descriptor describes the remote effect, independently
        # of the generic MCP transport kind. Include the safe scope in the hash.
        if metadata.get("access") in {"read", "write"}:
            action["access"] = metadata["access"]
        if isinstance(metadata.get("target"), str) and metadata["target"].strip():
            action["target"] = _normalized_text(metadata["target"])
        if "connector_context" in metadata:
            action["connector_context"] = normalize_connector_approval_context(
                metadata["connector_context"]
            )
    if kind == "computer_use":
        action["computer_use"] = {
            "arguments_digest": metadata.get("arguments_digest"),
            "scope_digest": metadata.get("scope_digest"),
            "scope": "turn",
        }
    if command is None and not files:
        action["summary"] = _normalized_text(
            getattr(request, "preview", None) or getattr(request, "reason", None) or kind
        )
    # Renderer-visible action data is redacted before hashing. The paused in-memory
    # request remains authoritative; the fingerprint binds the exact safe display
    # projection and cannot be used to smuggle replacement arguments back into runtime.
    return _json_clone(redact_secrets(action))


def normalize_connector_approval_context(value: Any) -> dict[str, Any]:
    """Closed safe display fields; never serialize arbitrary tool arguments."""
    fields = {"connector", "account", "repository", "service", "tool", "resources"}
    if (
        not isinstance(value, dict)
        or not set(value) <= fields
        or value.get("connector") not in ("GitHub", "Google", "Custom", "Verified")
    ):
        raise ValueError("Invalid connector approval context.")
    result = {"connector": value["connector"]}
    for key in ("account", "repository", "service", "tool"):
        if key not in value:
            continue
        item = value[key]
        if not isinstance(item, str) or not item.strip() or len(item) > 2000:
            raise ValueError("Invalid connector approval label.")
        result[key] = item
    if "resources" in value:
        resources = value["resources"]
        if (
            not isinstance(resources, list)
            or len(resources) > 100
            or any(
                not isinstance(item, str) or not item.strip() or len(item) > 2000
                for item in resources
            )
        ):
            raise ValueError("Invalid connector approval resources.")
        result["resources"] = list(resources)
    return _json_clone(redact_secrets(result))


def approval_policy_fingerprint(
    *,
    mode: str,
    kind: str,
    destructive: bool,
    allow_for_session_supported: bool,
) -> str:
    return sha256_canonical_json(
        {
            "mode": _normalized_kind(mode),
            "kind": _normalized_kind(kind),
            "destructive": bool(destructive),
            "allow_for_session_supported": bool(allow_for_session_supported),
        }
    )


def sha256_canonical_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def runtime_instance_fingerprint(instance_id: str, *, version: str) -> str:
    return sha256_canonical_json(
        {
            "contract": APPROVAL_BINDING_CONTRACT,
            "process_id": os.getpid(),
            "runtime_version": _normalized_identifier(version),
            "instance_id": _normalized_identifier(instance_id),
        }
    )


def _canonical_path(value: Any, *, base: str | None) -> str:
    text = _normalized_text(value)
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = Path(base or os.curdir) / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = Path(os.path.abspath(os.fspath(candidate)))
    return unicodedata.normalize("NFC", os.fspath(resolved))


def _normalized_text(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or "").replace("\r\n", "\n").strip())


def _normalized_kind(value: Any) -> str:
    return _normalized_text(value or "approval").casefold() or "approval"


def _normalized_identifier(value: Any) -> str:
    return _normalized_text(value)


def _optional_identifier(value: Any) -> str | None:
    normalized = _normalized_identifier(value)
    return normalized or None


def _tool_identity(metadata: dict[str, Any]) -> dict[str, str]:
    identity: dict[str, str] = {}
    for key in (
        "mcp_server_id",
        "server_id",
        "mcp_tool_id",
        "tool_id",
        "custom_tool_id",
        "custom_tool_name",
    ):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            identity[key] = _normalized_text(value)
    custom_tool = metadata.get("custom_tool")
    if isinstance(custom_tool, dict):
        for source, target in (("name", "custom_tool_name"), ("id", "custom_tool_id")):
            value = custom_tool.get(source)
            if isinstance(value, str) and value.strip():
                identity.setdefault(target, _normalized_text(value))
    return _json_clone(redact_secrets(identity))


def _access_for_kind(kind: str) -> str:
    if kind == "fs_delete" or "delete" in kind:
        return "delete"
    if kind.startswith("fs_") or kind in {"git_apply_patch"}:
        return "write"
    if kind.startswith(("shell_", "verify_", "custom_tool_run", "mcp_tool_run")):
        return "execute"
    if "network" in kind or kind == "workspace_preview_lan":
        return "network"
    if "read" in kind or "list" in kind:
        return "read"
    return "control"


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, sort_keys=True, default=str))
