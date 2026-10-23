"""Provider-independent, bounded tool details for the transcript."""

from __future__ import annotations

import json
import re

from prompt_toolkit.utils import get_cwidth

from ...error_text import sanitize_error_text_for_output
from ...ide.protocol import redact_secrets
from ...surface.types import PatchEvent
from .links import link_fragments, link_segments

MAX_CHARS = 16_000
MAX_LINES = 160
_ANSI = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~])")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_MUTED = "class:tui.diff.muted"
_ADD = "class:tui.diff.add"
_REMOVE = "class:tui.diff.remove"
_ACTIVE = "class:tui.trace.active"
_SUCCESS = "class:tui.trace.success"
_WARNING = "class:tui.trace.warning"
_DETAIL = "class:tui.trace.detail"
_COMMAND = "class:tui.trace.command"
_AGENT = "class:tui.trace.agent"
_COUNTS = re.compile(r"(\+\d+)( )(-\d+)$")
_DURATION = re.compile(r" (\([^()]*\d+(?:ms|s)[^()]*\))$")


def tool_is_view_only(name: str) -> bool:
    from ...tools.registry import get_builtin_tool_metadata

    spec = get_builtin_tool_metadata(name)
    return bool(
        spec
        and "read" in spec.categories
        and {"fs", "search", "git", "asset"}.intersection(spec.categories)
        and not {"write", "shell", "web"}.intersection(spec.categories)
    )


def clean_text(text: str) -> str:
    text = _ANSI.sub("", text).replace("\r\n", "\n")
    text = "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) < 160))
    return sanitize_error_text_for_output(redact_secrets(text))


def bounded_text(text: str) -> str:
    clean = clean_text(text)
    lines = clean[:MAX_CHARS].split("\n")
    visible = "\n".join(lines[:MAX_LINES])
    if len(clean) > MAX_CHARS or len(lines) > MAX_LINES:
        visible += "\n… Trace truncated; remaining content omitted."
    return visible


def tool_output(chunk: str) -> str:
    """Decode text fields without turning source indentation into JSON escapes."""
    try:
        value = redact_secrets(json.loads(chunk))
    except (ValueError, TypeError):
        return bounded_text(chunk)
    if isinstance(value, dict):
        parts: list[str] = []
        metadata = {}
        for key, item in value.items():
            if key in {"stdout", "stderr", "content", "output", "text", "diff"} and isinstance(
                item, str
            ):
                if item:
                    parts.append(f"{key}:\n{item}")
            else:
                metadata[key] = item
        if metadata:
            parts.insert(0, json.dumps(metadata, ensure_ascii=False, indent=2))
        return bounded_text("\n".join(parts))
    return bounded_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))


def _diff_lines(diff: str):
    """Keep file headers; use hunk lengths so +++ source isn't a file header."""
    old = new = old_left = new_left = 0
    for line in diff.splitlines():
        match = _HUNK.match(line)
        if match:
            old, old_left, new, new_left = (
                int(match[1]),
                int(match[2] or 1),
                int(match[3]),
                int(match[4] or 1),
            )
            yield _MUTED, "", line
        elif old_left and line.startswith("-"):
            yield _REMOVE, str(old), line
            old += 1
            old_left -= 1
        elif new_left and line.startswith("+"):
            yield _ADD, str(new), line
            new += 1
            new_left -= 1
        elif old_left and new_left and line.startswith(" "):
            yield "", str(new), line
            old += 1
            new += 1
            old_left -= 1
            new_left -= 1
        else:
            yield _MUTED, "", line


def patch_entry(event: PatchEvent) -> str:
    added = removed = 0
    for style, number, _ in _diff_lines(event.diff):
        if number:
            added += int(style == _ADD)
            removed += int(style == _REMOVE)
    who = ""
    if event.subagent_name:
        who = f" · {event.subagent_name} [{event.workspace_view}]"
    state = "Applied" if event.applied else "Preview"
    counts = f"+{added} -{removed}" if added or removed else "no text changes"
    if event.diff_truncated:
        counts = "partial diff · counts unavailable"
    header = f"✎ {state}{who} · {', '.join(event.files) or 'patch'} · {counts}"
    return json.dumps(
        {
            "header": bounded_text(header),
            "diff": bounded_text(event.diff),
            "applied": event.applied,
        },
        ensure_ascii=False,
    )


def _styled_rows(parts: list[tuple[str, str]], width: int) -> list[list[tuple[str, str]]]:
    """Wrap styled text by terminal cells without losing code spaces or links."""
    rows: list[list[tuple[str, str]]] = [[]]
    width = max(1, width)
    cells = 0
    for style, text in parts:
        for char in text.expandtabs(4):
            if char == "\n":
                rows.append([])
                cells = 0
                continue
            size = max(0, get_cwidth(char))
            if cells and cells + size > width:
                rows.append([])
                cells = 0
            if rows[-1] and rows[-1][-1][0] == style:
                rows[-1][-1] = (style, rows[-1][-1][1] + char)
            else:
                rows[-1].append((style, char))
            cells += size
    return rows


def preformatted_rows(text: str, width: int, style: str = "") -> list[list[tuple[str, str]]]:
    return _styled_rows([(style, text)], width)


def _detail_rows(parts: list[tuple[str, str]], width: int) -> list[list[tuple[str, str]]]:
    # The quiet rail keeps long output visibly attached to its tool. Reserve it
    # before wrapping so narrow terminals never lose the rightmost code column.
    rail = [(_MUTED, "│ ")] if width > 3 else []
    return [rail + row for row in _styled_rows(parts, width - (2 if rail else 0))]


def trace_rows(text: str, width: int) -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    for line in text.split("\n"):
        mark = line.lstrip()[:1]
        style = {"▸": _ACTIVE, "✓": _SUCCESS, "↳": _SUCCESS, "◦": _WARNING}.get(mark, _MUTED)
        clock = _DURATION.search(line)
        body = line[: clock.start()] if clock else line
        title, separator, detail = body.partition(" · ")
        if "(view only)" in title and mark in {"▸", "✓", "↳"}:
            style = _ACTIVE
        parts = [(style, title)]
        if separator:
            parts.extend([(_MUTED, separator), (_DETAIL, detail)])
        if clock:
            parts.append((_MUTED, line[clock.start() :]))
        linked = [
            fragment
            for part_style, part in parts
            for fragment in link_fragments(part_style, link_segments(part, [part])[0])
        ]
        rows.extend(_styled_rows(linked, width))
    return rows


def output_rows(text: str, width: int) -> list[list[tuple[str, str]]]:
    if text.startswith("$ "):
        return _detail_rows([(_COMMAND, text)], width)
    heading, separator, body = text.partition("\n")
    rows = preformatted_rows(heading, width, _ACTIVE)
    view_only = heading.startswith("VIEWING ONLY · ")
    if separator:
        parts: list[tuple[str, str]] = []
        for index, line in enumerate(body.split("\n")):
            if index:
                parts.append(("", "\n"))
            style = _DETAIL
            if line == "stderr:" or line.startswith("… Trace truncated;"):
                style = _WARNING
            elif line in {"stdout:", "content:", "output:", "text:", "diff:"}:
                style = _MUTED
                if view_only and line in {"content:", "text:"}:
                    line = "Viewed content — unchanged:"
                    style = _ACTIVE
            parts.append((style, line))
        rows.extend(_detail_rows(parts, width))
    return rows


def patch_rows(entry: str, width: int, *, expanded: bool) -> list[list[tuple[str, str]]]:
    value = json.loads(entry)
    header = value["header"]
    title, separator, detail = header.partition(" · ")
    applied = value.get("applied", title.startswith("✎ Applied"))
    title = "✓ CHANGES SAVED" if applied else "◇ PROPOSED CHANGES"
    parts = [(_SUCCESS if applied else _WARNING, title)]
    if separator:
        parts.append((_MUTED, separator))
        counts = _COUNTS.search(detail)
        description = detail[: counts.start()] if counts else detail
        agent, agent_sep, remainder = description.partition("] · ")
        if agent_sep and "[" in agent:
            parts.extend([(_AGENT, agent + "]"), (_MUTED, " · ")])
            description = remainder
        parts.append((_WARNING if "counts unavailable" in description else _DETAIL, description))
        if counts:
            added, removed = int(counts[1]), -int(counts[3])
            add_label = "added" if applied else "to add"
            remove_label = "removed" if applied else "to remove"
            parts.extend(
                [
                    (_ADD, f"{added} {'line' if added == 1 else 'lines'} {add_label}"),
                    (_MUTED, " · "),
                    (_REMOVE, f"{removed} {'line' if removed == 1 else 'lines'} {remove_label}"),
                ]
            )
    rows = _styled_rows(parts, width)
    if not applied:
        rows.extend(
            preformatted_rows("Preview only — these changes have not been saved.", width, _WARNING)
        )
    if expanded and value["diff"]:
        lines = list(_diff_lines(value["diff"]))
        gutter = max((len(number) for _, number, _ in lines), default=1)
        for style, number, text in lines:
            if number:
                label = (
                    ("ADDED" if applied else "ADD")
                    if style == _ADD
                    else ("REMOVED" if applied else "REMOVE")
                    if style == _REMOVE
                    else "UNCHANGED"
                )
                # Repeat the label on wrapped rows so code remains attributable
                # when a long line fills the viewport or the heading scrolls out.
                prefix = [(style or _MUTED, f"{label:<9} "), (_MUTED, f"{number:>{gutter}} ")]
                prefix_width = 11 + gutter
                rail_width = 2 if width > 3 else 0
                if width > prefix_width + rail_width + 2:
                    code_rows = preformatted_rows(
                        text, width - prefix_width - rail_width, style or _MUTED
                    )
                    for row in code_rows:
                        rows.extend(_detail_rows(prefix + row, width))
                else:
                    rows.extend(_detail_rows(prefix + [(style or _MUTED, text)], width))
                continue
            match = _HUNK.match(text)
            if match:
                if match[2] == "0":
                    location = f"Added at line {match[3]}"
                elif match[4] == "0":
                    location = f"Removed from line {match[1]}"
                else:
                    location = f"Before: line {match[1]} · After: line {match[3]}"
                rows.extend(_detail_rows([(_MUTED, location)], width))
            elif text.startswith(("diff --git ", "index ", "--- ")):
                continue
            elif text.startswith("+++ "):
                path = text[4:]
                if path.startswith("b/"):
                    path = path[2:]
                if path != "/dev/null":
                    rows.extend(_detail_rows([(_ACTIVE, f"File: {path}")], width))
            else:
                style = _WARNING if text.startswith("… Trace truncated;") else _MUTED
                rows.extend(_detail_rows([(style, text)], width))
    return rows
