from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

# Trace reason for results that only say the remote site blocked the call.
DEFAULT_REMOTE_SITE_REASON = "site doesn't allow automated access"


def remote_site_outcome(meta: Mapping[str, Any] | None) -> str:
    """Trace reason when a failed tool call was the remote site's doing, else "".

    A page the site refused, stalled, or dropped is an outcome of that source,
    not an agent failure: surfaces show it as a neutral trace line rather than
    an error. Approval declines are never softened.
    """
    if not isinstance(meta, Mapping) or meta.get("approval_declined"):
        return ""
    reason = " ".join(str(meta.get("remote_site_reason") or "").split())
    if reason:
        return reason
    if meta.get("blocked_by_remote_site"):
        return DEFAULT_REMOTE_SITE_REASON
    return ""


@dataclass(frozen=True, slots=True)
class StatusEvent:
    mode: str
    model: str
    workspace: str
    session_id: str
    branch: str
    dirty: bool
    stream: bool
    task: str = "-"


@dataclass(frozen=True, slots=True)
class ToolStartEvent:
    tool_call_id: str
    name: str
    args: dict[str, Any]
    step: int
    subagent_name: str | None = None
    subagent_mode: str | None = None
    nesting_depth: int = 0


@dataclass(frozen=True, slots=True)
class ToolOutputEvent:
    tool_call_id: str
    name: str
    chunk: str
    subagent_name: str | None = None
    subagent_mode: str | None = None
    nesting_depth: int = 0


@dataclass(frozen=True, slots=True)
class ToolEndEvent:
    tool_call_id: str
    name: str
    status: str
    elapsed_ms: int
    meta: dict[str, Any] = field(default_factory=dict)
    subagent_name: str | None = None
    subagent_mode: str | None = None
    nesting_depth: int = 0
    # Time spent waiting on a human approval prompt during this tool call.
    # ``elapsed_ms`` is the tool's ACTIVE runtime (wait already subtracted);
    # this field carries the wait so surfaces can disclose it separately
    # instead of billing the user's decision time to the tool.
    approval_wait_ms: int = 0


@dataclass(frozen=True, slots=True)
class SubagentStartEvent:
    name: str
    mode: str
    subagent_session_id: str | None = None
    # What the subagent is for (the definition's description). Surfaces may show a
    # condensed form so the user knows what agent they just entered.
    description: str = ""
    subagent_run_id: str | None = None
    workspace_view: str = "shared"
    label: str = ""


@dataclass(frozen=True, slots=True)
class SubagentEndEvent:
    name: str
    mode: str
    status: str
    elapsed_ms: int
    steps_completed: int
    subagent_session_id: str | None = None
    error: str | None = None
    subagent_run_id: str | None = None
    workspace_view: str = "shared"
    label: str = ""


@dataclass(frozen=True, slots=True)
class PatchEvent:
    files: list[str]
    diff: str
    summary: str = ""
    applied: bool = False
    diff_truncated: bool = False
    subagent_name: str | None = None
    workspace_view: str = "shared"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    kind: str
    reason: str
    preview: str
    files: list[str] = field(default_factory=list)
    command: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    allow_for_session_scope: dict[str, object] | None = None


def approval_review_line(outcome: str, subject: str, reason: str = "") -> str:
    """One readable line for an approval the reviewer answered ("Approve for me")."""

    lead = {
        "allow": "Approved for you",
        "deny": "Not approved",
        "ask_person": "Asking you",
    }.get(outcome, "Reviewed")
    line = f"{lead}: {subject}"
    return f"{line} - {reason}" if reason else line


@dataclass(frozen=True, slots=True)
class StandingApproval:
    """What a surface's standing rules already decide about a request, before anyone is asked.

    Surfaces may offer ``standing_approval(request) -> StandingApproval | None``: ``allow`` or
    ``deny`` when a stored grant or permission rule decides the request, ``ask_person`` when a
    rule says a person must answer this one (an "ask" rule, a folder outside the project), and
    ``None`` when no rule applies. "Approve for me" consults it first, so a person's rules
    always win over the reviewer.
    """

    outcome: Literal["allow", "deny", "ask_person"]
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    allow: bool
    allow_for_session: bool = False
    # Folder-wide session grant: the user chose "always for this
    # folder" - the surface stores an (operation, directory) grant and
    # auto-approves later same-operation requests whose files all fall under
    # it. Only offered where approval_scope.approval_dir_grant_candidate says
    # so (fs_delete, single non-root directory, never sensitive approvals).
    allow_for_session_dir: bool = False
    status: str | None = None
