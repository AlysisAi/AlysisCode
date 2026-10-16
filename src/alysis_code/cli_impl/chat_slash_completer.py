from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.completion import Completer, Completion

from .chat.forge_visibility import command_visible
from .tui.actions import MODEL_DESCRIPTION


@dataclass(frozen=True)
class ChatSlashCommandSpec:
    name: str
    usage: str
    description: str


NAVIGATION_COMMAND_SPECS = (
    ChatSlashCommandSpec("mcp", "/mcp", "Inspect MCP state, tools and connection controls"),
    ChatSlashCommandSpec(
        "extensions", "/extensions", "Inspect installed extensions and configuration"
    ),
    ChatSlashCommandSpec("plan", "/plan", "Choose Architect or Forge planning"),
    ChatSlashCommandSpec("planning", "/planning", "Planning choices and return to previous mode"),
    ChatSlashCommandSpec("find", "/find [text]", "Find text in this transcript"),
    ChatSlashCommandSpec("copy-last", "/copy-last", "Copy last assistant response"),
)

DIFF_COMMAND_SPEC = ChatSlashCommandSpec("diff", "/diff", "Browse working-tree changes locally")
REVIEW_COMMAND_SPEC = ChatSlashCommandSpec(
    "review", "/review", "Choose a scope for structured review"
)


@dataclass(frozen=True)
class _ChatSlashCompletionSpec:
    completion: str
    usage: str
    description: str


_DISPLAY_WIDTH = 72
_USAGE_COLUMN = 34


def _ellipsize(text: str, limit: int) -> str:
    clean = " ".join(str(text or "").split())
    limit = max(1, int(limit))
    if len(clean) <= limit:
        return clean
    if limit <= 3:
        return "." * limit
    clipped = clean[: limit - 3]
    # Don't cut mid-word - when the cut lands inside a word and at least
    # one whole word fits, retreat to the last word boundary before eliding.
    if clean[limit - 3 : limit - 2] not in ("", " ") and " " in clipped:
        head, _, _tail = clipped.rpartition(" ")
        if head:
            clipped = head
    return clipped.rstrip().rstrip(",;:") + "..."


def _completion_display(
    usage: str,
    description: str,
    *,
    width: int = _DISPLAY_WIDTH,
) -> str:
    """Return one bounded completion row for prompt_toolkit's menu."""
    width = max(8, int(width))
    usage_text = " ".join(str(usage or "").split())
    desc_text = " ".join(str(description or "").split())
    if width <= _USAGE_COLUMN + 1:
        return _ellipsize(usage_text, width)
    usage_cell = _ellipsize(usage_text, _USAGE_COLUMN)
    prefix = f"{usage_cell:<{_USAGE_COLUMN}} "
    remaining = max(1, width - len(prefix))
    return (prefix + _ellipsize(desc_text, remaining))[:width]


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
    ChatSlashCommandSpec("forge", "/forge [resume]", "Enter or resume Forge"),
    ChatSlashCommandSpec("report", "/report [text]", "Create feedback bundle and issue draft"),
    ChatSlashCommandSpec("feedback", "/feedback [text]", "Alias for /report"),
    ChatSlashCommandSpec(
        "skill",
        "/skill",
        "No args lists; <name> shows info; <name> <task> attaches",
    ),
    ChatSlashCommandSpec("exit", "/exit", "Exit chat"),
)

FORGE_SPECS: tuple[ChatSlashCommandSpec, ...] = (
    ChatSlashCommandSpec("assistant", "/assistant on|off|status", "Toggle planner assistant"),
    ChatSlashCommandSpec("assets", "/assets", "Open Forge assets"),
    ChatSlashCommandSpec("execute", "/execute plan", "Run scoped planned tasks"),
    ChatSlashCommandSpec("goal", "/goal <text>", "Set project goal"),
    ChatSlashCommandSpec("task", "/task <title>", "Add a task to the plan"),
    ChatSlashCommandSpec("show", "/show", "Show the current plan summary"),
    ChatSlashCommandSpec("plan", "/plan tasks|markdown|edit", "View or edit the plan"),
    ChatSlashCommandSpec("done", "/done", "Save and validate the plan"),
    ChatSlashCommandSpec("back", "/back", "Return to chat without finalizing"),
)

_SHARED_NESTED_SPECS: tuple[_ChatSlashCompletionSpec, ...] = (
    _ChatSlashCompletionSpec("/usage hud", "/usage hud", "Open usage HUD controls"),
    _ChatSlashCompletionSpec("/usage hud on", "/usage hud on", "Enable the persistent usage HUD"),
    _ChatSlashCompletionSpec(
        "/usage hud off", "/usage hud off", "Disable the persistent usage HUD"
    ),
    _ChatSlashCompletionSpec(
        "/usage hud status", "/usage hud status", "Show the current usage HUD state"
    ),
    _ChatSlashCompletionSpec("/terminals list", "/terminals list", "List background processes"),
    _ChatSlashCompletionSpec(
        "/terminals show", "/terminals show <process_id>", "Show background process output"
    ),
    _ChatSlashCompletionSpec(
        "/terminals kill", "/terminals kill <process_id>", "Kill a background process"
    ),
    _ChatSlashCompletionSpec("/terminals help", "/terminals help", "Show terminal command usage"),
    _ChatSlashCompletionSpec("/stream on", "/stream on", "Render model output live"),
    _ChatSlashCompletionSpec("/stream off", "/stream off", "Buffer model output until complete"),
    _ChatSlashCompletionSpec(
        "/stream status", "/stream status", "Show the current streaming setting"
    ),
)

_CHAT_NESTED_SPECS: tuple[_ChatSlashCompletionSpec, ...] = (
    _ChatSlashCompletionSpec("/forge resume", "/forge resume", "Resume the current run pointer"),
)

_FORGE_NESTED_SPECS: tuple[_ChatSlashCompletionSpec, ...] = (
    _ChatSlashCompletionSpec("/execute plan", "/execute plan", "Run scoped planned tasks"),
    _ChatSlashCompletionSpec("/plan tasks", "/plan tasks", "Show the plan task table"),
    _ChatSlashCompletionSpec(
        "/plan markdown", "/plan markdown", "Preview PLAN.md for the current run"
    ),
    _ChatSlashCompletionSpec("/plan md", "/plan md", "Preview PLAN.md for the current run"),
    _ChatSlashCompletionSpec("/plan edit", "/plan edit", "Edit plan.json and reload Forge state"),
)


def get_chat_specs() -> tuple[ChatSlashCommandSpec, ...]:
    return SPECS


def get_forge_specs() -> tuple[ChatSlashCommandSpec, ...]:
    return FORGE_SPECS


def max_completions_for_mode(mode: str) -> int:
    """Return the largest static slash completion list for one menu in this mode."""

    normalized = str(mode or "chat").strip().lower()
    if normalized == "forge":
        top_level_count = len(
            [spec for spec in SPECS if spec.name not in {"clear", "forge", "plan"}]
        )
        top_level_count += len(FORGE_SPECS)
        nested_count = len(_SHARED_NESTED_SPECS) + len(_FORGE_NESTED_SPECS)
        return max(top_level_count, nested_count)

    top_level_count = len(SPECS)
    nested_count = len(_SHARED_NESTED_SPECS) + len(_CHAT_NESTED_SPECS)
    return max(top_level_count, nested_count)


class ChatSlashCompleter(Completer):
    def __init__(
        self,
        *,
        mode_provider: Callable[[], str],
        skill_names_provider: Callable[[], Sequence[str]] | None = None,
        extra_specs: Sequence[ChatSlashCommandSpec] = (),
    ) -> None:
        self._mode_provider = mode_provider
        self._skill_names_provider = skill_names_provider
        self._extra_specs = tuple(extra_specs)

    def get_completions(self, document: Any, complete_event: Any) -> Iterable[Completion]:
        del complete_event
        text = str(getattr(document, "text_before_cursor", "") or "")
        if text.startswith("$"):
            completions = self._matching_dollar_skill_completions(text)
            for spec in completions:
                yield Completion(
                    text=spec.completion,
                    start_position=-len(text),
                    display=_completion_display(spec.usage, spec.description),
                )
            return
        if not text.startswith("/"):
            return
        completions = self._matching_completions(text)
        for spec in completions:
            yield Completion(
                text=spec.completion,
                start_position=-len(text),
                display=_completion_display(spec.usage, spec.description),
            )

    def _matching_completions(self, text: str) -> list[_ChatSlashCompletionSpec]:
        entries = self._completion_entries_for_text(text)
        text_lower = text.lower()
        matches = [entry for entry in entries if entry.completion.lower().startswith(text_lower)]
        exact = [entry for entry in matches if entry.completion.lower() == text_lower]
        inexact = [entry for entry in matches if entry.completion.lower() != text_lower]
        return [*exact, *inexact]

    def _matching_dollar_skill_completions(self, text: str) -> list[_ChatSlashCompletionSpec]:
        if any(char.isspace() for char in text):
            return []
        text_lower = text.casefold()
        return [
            entry
            for entry in self._skill_name_entries(prefix="$")
            if entry.completion.casefold().startswith(text_lower)
        ]

    def _completion_entries_for_text(self, text: str) -> list[_ChatSlashCompletionSpec]:
        if " " not in text[1:]:
            return self._top_level_entries()
        return self._nested_entries(text)

    def _top_level_entries(self) -> list[_ChatSlashCompletionSpec]:
        specs = list(get_chat_specs())
        specs.extend(self._extra_specs)
        mode = self._mode()
        if mode == "forge":
            specs = [spec for spec in specs if spec.name not in {"clear", "forge", "plan"}]
            specs.extend(get_forge_specs())
        planning_entry = any(spec.name == "plan" for spec in self._extra_specs)
        specs = [
            spec
            for spec in specs
            if command_visible(f"/{spec.name}", ui_mode=mode)
            or (planning_entry and spec.name == "plan")
        ]
        return [
            _ChatSlashCompletionSpec(
                completion=f"/{spec.name}",
                usage=spec.usage,
                description=spec.description,
            )
            for spec in specs
        ]

    def _nested_entries(self, text: str) -> list[_ChatSlashCompletionSpec]:
        entries = list(_SHARED_NESTED_SPECS)
        if self._mode() == "forge":
            entries.extend(_FORGE_NESTED_SPECS)
        else:
            entries.extend(_CHAT_NESTED_SPECS)
        if text.lower().startswith("/skill "):
            entries.extend(self._skill_name_entries(prefix="/skill "))
        return entries

    def _skill_name_entries(self, *, prefix: str) -> list[_ChatSlashCompletionSpec]:
        names = self._safe_names(self._skill_names_provider)
        return [
            _ChatSlashCompletionSpec(
                completion=f"{prefix}{name}",
                usage=f"{prefix}{name} [task]",
                description="Show skill info or attach it for one turn",
            )
            for name in names
        ]

    @staticmethod
    def _safe_names(provider: Callable[[], Sequence[str]] | None) -> list[str]:
        if provider is None:
            return []
        try:
            values = provider()
        except Exception:
            return []
        seen: set[str] = set()
        ordered: list[str] = []
        for value in values:
            name = str(value or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            ordered.append(name)
        return ordered

    def _mode(self) -> str:
        try:
            mode = str(self._mode_provider() or "chat").strip().lower()
        except Exception:
            return "chat"
        return "forge" if mode == "forge" else "chat"
