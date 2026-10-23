"""Pure row construction for the terminal TUI's live subagent panel.

The panel is a bounded live tail, not a transcript.  This module deliberately
owns no prompt_toolkit state: callers supply the latest child status and the
entries accumulated from incremental scheduler reads.
"""

from __future__ import annotations

from typing import Any

from ...subagent_labels import subagent_identity

ENTRY_BUFFER_CAP = 240

_MIDDLE_DOT = "\u00b7"
_ELLIPSIS = "\u2026"

_S_HEAD = "class:tui.subpanel.head"
_S_STATE = "class:tui.subpanel.state"
_S_DIM = "class:tui.subpanel.dim"
_S_TOOL = "class:tui.subpanel.tool"
_S_RESULT = "class:tui.subpanel.result"
_S_ASSISTANT = "class:tui.subpanel.assistant"
_S_USER = "class:tui.subpanel.user"
_S_HINT = "class:tui.subpanel.hint"

_ENTRY_STYLE = {
    "tool": (_S_TOOL, "\u25b8 "),
    "tool_result": (_S_RESULT, "\u2713 "),
    "tool_error": (_S_RESULT, "\u2717 "),
    "assistant": (_S_ASSISTANT, "a: "),
    "user": (_S_USER, "u: "),
}


def append_bounded_entries(
    existing: list[dict[str, str]],
    incoming: list[dict[str, str]],
    *,
    limit: int = ENTRY_BUFFER_CAP,
) -> list[dict[str, str]]:
    """Append normalized entries while retaining only the newest ``limit``."""
    cap = max(1, int(limit))
    combined = [*existing, *incoming]
    return combined[-cap:]


def _fit_segments(segments: list[tuple[str, str]], width: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    used = 0
    for style, value in segments:
        text = str(value or "")
        if not text:
            continue
        room = width - used
        if room <= 0:
            break
        if len(text) <= room:
            out.append((style, text))
            used += len(text)
            continue
        clipped = text[: room - 1].rstrip() + _ELLIPSIS if room >= 2 else _ELLIPSIS
        out.append((style, clipped[:room]))
        break
    return out


def _elapsed_label(value: Any) -> str:
    try:
        elapsed_ms = max(0, int(value or 0))
    except (TypeError, ValueError):
        elapsed_ms = 0
    if elapsed_ms < 1000:
        return f"{elapsed_ms}ms"
    seconds = elapsed_ms // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def quiet_time_label(status: dict[str, Any]) -> str:
    """Return a deterministic quiet-age label once the existing interval passes."""
    if str(status.get("state") or "").strip().lower() != "running":
        return ""
    try:
        age_s = max(0.0, float(status.get("last_event_age_s") or 0.0))
        threshold_s = max(0.0, float(status.get("activity_threshold_s") or 0.0))
    except (TypeError, ValueError):
        return ""
    if threshold_s <= 0.0 or age_s < threshold_s:
        return ""
    return f"quiet {_elapsed_label(int(age_s * 1000))}"


def _header_row(
    status: dict[str, Any],
    *,
    width: int,
    position: int,
    total: int,
) -> list[tuple[str, str]]:
    name = subagent_identity(
        status.get("subagent") or status.get("run_id") or "subagent",
        status.get("label"),
    )
    state = str(status.get("state") or "unknown").strip()
    workspace = str(status.get("workspace_view") or "shared").strip()
    try:
        steps = max(0, int(status.get("steps_completed") or 0))
    except (TypeError, ValueError):
        steps = 0
    separator = f" {_MIDDLE_DOT} "
    quiet = quiet_time_label(status)
    return _fit_segments(
        [
            (_S_HEAD, name),
            (_S_DIM, separator),
            (_S_STATE, state),
            (_S_DIM, separator),
            (_S_DIM, _elapsed_label(status.get("elapsed_ms"))),
            (_S_DIM, separator),
            (_S_DIM, f"{steps} step{'s' if steps != 1 else ''}"),
            (_S_DIM, separator),
            (_S_DIM, workspace),
            (_S_DIM, separator if quiet else ""),
            (_S_DIM, quiet),
            (_S_DIM, f"    view {max(1, position)}/{max(1, total)}"),
        ],
        width,
    )


def _entry_rows(entry: dict[str, str], width: int) -> list[list[tuple[str, str]]]:
    kind = str(entry.get("kind") or "").strip()
    style, lead = _ENTRY_STYLE.get(kind, (_S_DIM, "- "))
    summary = " ".join(str(entry.get("summary") or "").split())
    return [_fit_segments([(_S_DIM, lead), (style, summary)], width)]


def subagent_panel_rows(
    status: dict[str, Any],
    entries: list[dict[str, str]],
    *,
    width: int,
    height: int,
    position: int,
    total: int,
    key_hint: str | None = None,
) -> list[list[tuple[str, str]]]:
    """Render header, newest body entries, and footer within ``height`` rows."""
    width = max(12, int(width))
    height = max(2, int(height))
    body_height = max(0, height - 2)
    selected: list[list[list[tuple[str, str]]]] = []
    used = 0
    for entry in reversed(entries):
        wrapped = _entry_rows(entry, width)
        if used and used + len(wrapped) > body_height:
            break
        room = body_height - used
        if room <= 0:
            break
        selected.append(wrapped[:room])
        used += min(len(wrapped), room)
    body_rows = [row for group in reversed(selected) for row in group]
    footer = _fit_segments(
        [(_S_HINT, key_hint or f"alt+left / alt+right to switch {_MIDDLE_DOT} esc to close")],
        width,
    )
    return [
        _header_row(status, width=width, position=position, total=total),
        *body_rows,
        footer,
    ]


__all__ = [
    "ENTRY_BUFFER_CAP",
    "append_bounded_entries",
    "quiet_time_label",
    "subagent_panel_rows",
]
