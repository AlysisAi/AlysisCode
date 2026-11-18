"""Command metadata shared by the terminal completer and IDE catalog."""

from __future__ import annotations

from dataclasses import dataclass

from .tui.actions import MODEL_DESCRIPTION


@dataclass(frozen=True)
class ChatSlashCommandSpec:
    name: str
    usage: str
    description: str


SPECS: tuple[ChatSlashCommandSpec, ...] = (
    ChatSlashCommandSpec("help", "/help", "Show available commands"),
    ChatSlashCommandSpec("permissions", "/permissions", "Change execution mode"),
    ChatSlashCommandSpec("persona", "/persona", "Switch persona: code, architect, ask, debug"),
    ChatSlashCommandSpec("ask", "/ask <question>", "One read-only turn; mode restored after"),
    ChatSlashCommandSpec("status", "/status", "Show session status"),
    ChatSlashCommandSpec("subagents", "/subagents", "Open an active subagent run"),
    ChatSlashCommandSpec("terminals", "/terminals [list|show|kill]", "Manage background processes"),
    ChatSlashCommandSpec("pwd", "/pwd", "Show active workdir"),
    ChatSlashCommandSpec("usage", "/usage", "Token count & cost; /usage hud on|off toggles HUD"),
    ChatSlashCommandSpec("context", "/context", "Show context usage"),
    ChatSlashCommandSpec("compact", "/compact [focus]", "Force conversation compaction"),
    ChatSlashCommandSpec("clear", "/clear", "Wipe conversation; keep session id and log"),
    ChatSlashCommandSpec("resume", "/resume [id]", "Resume a previous session"),
    ChatSlashCommandSpec("stream", "/stream", "Toggle live answer and reasoning output"),
    ChatSlashCommandSpec("trace", "/trace", "Set reasoning trace detail"),
    ChatSlashCommandSpec("model", "/model [name]", MODEL_DESCRIPTION),
    ChatSlashCommandSpec("config", "/config", "Open or show configuration"),
    ChatSlashCommandSpec("login", "/login", "Connect your Alysis Code account or a subscription"),
    ChatSlashCommandSpec("logout", "/logout", "Disconnect your Alysis Code account"),
    ChatSlashCommandSpec("toolbar", "/toolbar", "Configure status toolbar"),
    ChatSlashCommandSpec("image", "/image [path]", "Attach an image or file to the next message"),
    ChatSlashCommandSpec("report", "/report [text]", "Create feedback bundle and issue draft"),
    ChatSlashCommandSpec("feedback", "/feedback [text]", "Alias for /report"),
    ChatSlashCommandSpec(
        "skill",
        "/skill",
        "No args lists; <name> shows info; <name> <task> attaches",
    ),
    ChatSlashCommandSpec("exit", "/exit", "Exit chat"),
)


def get_chat_specs() -> tuple[ChatSlashCommandSpec, ...]:
    return SPECS


def command_rows() -> list[dict[str, str]]:
    specs = get_chat_specs()
    return [
        {"name": spec.name, "usage": spec.usage, "description": spec.description} for spec in specs
    ]
