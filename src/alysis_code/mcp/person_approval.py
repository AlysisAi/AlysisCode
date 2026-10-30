"""The desktop user's rule for their own MCP tool servers: every call asks a person.

The desktop app turns the rule on for its chat engines with
``ALYSIS_DESKTOP_TOOL_SERVER_APPROVAL``, a JSON object with:

- ``read_marks``: the file where the app records tools the *person* marked "only reads";
- ``exempt``: servers the app registers itself (Computer Use), by id and exact command.

A server's own ``readOnlyHint`` grants nothing under this rule. Other hosts (CLI, IDE
extensions) don't set the variable and keep the permission-mode rules.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .models import ResolvedMcpServer

TOOL_SERVER_APPROVAL_ENV = "ALYSIS_DESKTOP_TOOL_SERVER_APPROVAL"
MAX_READ_MARKS_BYTES = 256 * 1024
_MAX_MARKED_TOOLS = 512

CallApproval = Literal["permission_mode", "every_call", "marked_read"]


@dataclass(frozen=True)
class ToolServerApproval:
    exempt: frozenset[tuple[str, str]]
    read_marks: Path | None


def tool_server_approval(environ: Any = None) -> ToolServerApproval | None:
    """The desktop rule, or None when the host didn't turn it on."""
    raw = (os.environ if environ is None else environ).get(TOOL_SERVER_APPROVAL_ENV)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    # A value the engine can't read keeps the rule on, without exemptions or marks.
    if not isinstance(value, dict) or value.get("version") != 1:
        return ToolServerApproval(frozenset(), None)
    exempt = frozenset(
        (entry["server_id"], entry["command"])
        for entry in value.get("exempt") or []
        if isinstance(entry, dict)
        and isinstance(entry.get("server_id"), str)
        and isinstance(entry.get("command"), str)
        and entry["command"]
    )
    marks = value.get("read_marks")
    return ToolServerApproval(
        exempt, Path(marks) if isinstance(marks, str) and os.path.isabs(marks) else None
    )


def server_identity(server: ResolvedMcpServer) -> str:
    """What a person's marks are tied to: the transport, then the URL or command and arguments."""
    parts = (
        [server.transport, server.url or ""]
        if server.transport == "http"
        else [server.transport, server.command or "", *server.args]
    )
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _read_marks(path: Path) -> dict[str, Any]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return {}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_READ_MARKS_BYTES:
            return {}
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(MAX_READ_MARKS_BYTES + 1)
    except OSError:
        return {}
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > MAX_READ_MARKS_BYTES:
        return {}
    try:
        value = json.loads(data)
    except ValueError:
        return {}
    if not isinstance(value, dict) or value.get("version") != 1:
        return {}
    servers = value.get("servers")
    return servers if isinstance(servers, dict) else {}


def marked_read_tools(
    policy: ToolServerApproval | None, server: ResolvedMcpServer
) -> frozenset[str]:
    """Tools the person marked "only reads" for exactly this server; empty on any doubt."""
    if policy is None or policy.read_marks is None:
        return frozenset()
    entry = _read_marks(policy.read_marks).get(server.id)
    if not isinstance(entry, dict) or entry.get("identity") != server_identity(server):
        return frozenset()
    tools = entry.get("read_tools")
    if not isinstance(tools, list) or len(tools) > _MAX_MARKED_TOOLS:
        return frozenset()
    return frozenset(tool for tool in tools if isinstance(tool, str))


def exempt_from_rule(policy: ToolServerApproval, server: ResolvedMcpServer) -> bool:
    """The app's own Computer Use server, matched by id and the exact command it registered."""
    return server.transport == "stdio" and (server.id, server.command or "") in policy.exempt


def call_approval(
    policy: ToolServerApproval | None, server: ResolvedMcpServer | None, tool_name: str
) -> CallApproval:
    if policy is None:
        return "permission_mode"
    if server is None:
        return "every_call"
    if exempt_from_rule(policy, server):
        return "permission_mode"
    return "marked_read" if tool_name in marked_read_tools(policy, server) else "every_call"
