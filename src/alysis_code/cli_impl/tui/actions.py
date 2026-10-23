"""Action metadata shared by keyboard preferences and command discovery."""

from __future__ import annotations

from dataclasses import dataclass

MODEL_LABEL = "Model and reasoning"
MODEL_DESCRIPTION = "Select model and reasoning through provider configuration"


@dataclass(frozen=True)
class TuiAction:
    id: str
    label: str
    command: str
    capability: str
    modes: tuple[str, ...] = ("chat", "forge")
    mid_turn: bool = True
    description: str | None = None


DEFAULT_ACTIONS = (
    TuiAction("paste.edit", "Edit pasted text", "paste.edit", "paste-editor"),
    TuiAction("subagent.previous", "Previous subagent", "subagent.previous", "subagents"),
    TuiAction("subagent.close", "Close subagent view", "subagent.close", "subagent-view"),
    TuiAction("subagent.next", "Next subagent", "subagent.next", "subagents"),
    TuiAction("planmeta.toggle", "Expand/collapse plan metadata", "planmeta.toggle", "composer"),
    TuiAction("mcp", "Inspect MCP integrations", "/mcp", "integrations"),
    TuiAction("extensions", "Inspect extensions", "/extensions", "integrations"),
    TuiAction("planning", "Planning choices and return", "/planning", "planning", mid_turn=False),
    TuiAction(
        "sessions", "Search and resume conversations", "/resume", "session-search", mid_turn=False
    ),
    TuiAction("transcript.find", "Find in transcript", "/find", "transcript"),
    TuiAction("transcript.copy", "Copy last assistant response", "/copy-last", "transcript"),
    TuiAction(
        "review",
        "Structured code review",
        "/review",
        "review",
        mid_turn=False,
        description="Choose scope and explicitly start a read-only review",
    ),
    TuiAction("help", "Show command help", "/help", "help"),
    TuiAction(
        "diff",
        "Browse working-tree changes",
        "/diff",
        "diff",
        description="View staged, unstaged and untracked changes locally",
    ),
    TuiAction(
        "history",
        "Search prompt history",
        "history.search",
        "composer",
        description="Find and recall accepted prompts",
    ),
    TuiAction("model", MODEL_LABEL, "/model", "config"),
    TuiAction(
        "reasoning",
        "Expand/collapse reasoning summaries",
        "reasoning.toggle",
        "composer",
        description="Expand or collapse reasoning summaries",
    ),
    TuiAction("config", "Open configuration", "/config", "config"),
    TuiAction("permissions", "Change execution permissions", "/permissions", "/permissions"),
    TuiAction("persona", "Switch persona", "/persona", "/persona"),
    TuiAction("status", "Show session status", "/status", "/status"),
    TuiAction("usage", "Show token usage and cost", "/usage", "/usage"),
    TuiAction("context", "Show context usage", "/context", "/context"),
    TuiAction("stream", "Change streaming output", "/stream", "/stream"),
    TuiAction("trace", "Change reasoning trace detail", "/trace", "/trace"),
    TuiAction("forge", "Enter Forge", "/forge", "/forge", ("chat",), False),
    TuiAction("show", "Show Forge plan", "/show", "/show", ("forge",), False),
    TuiAction("back", "Return to chat", "/back", "runner", ("forge",), False),
)
