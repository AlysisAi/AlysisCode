from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.completion import Completer, Completion

from .chat_slash_catalog import SPECS, ChatSlashCommandSpec, get_chat_specs

NAVIGATION_COMMAND_SPECS = (
    ChatSlashCommandSpec("mcp", "/mcp", "Inspect MCP state, tools and connection controls"),
    ChatSlashCommandSpec(
        "extensions", "/extensions", "Inspect installed extensions and configuration"
    ),
    ChatSlashCommandSpec("plan", "/plan", "Plan with the Architect persona or return"),
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


def max_completions() -> int:
    """Return the largest static slash completion list for one menu."""
    return max(len(SPECS), len(_SHARED_NESTED_SPECS))


class ChatSlashCompleter(Completer):
    def __init__(
        self,
        *,
        skill_names_provider: Callable[[], Sequence[str]] | None = None,
        extra_specs: Sequence[ChatSlashCommandSpec] = (),
    ) -> None:
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
