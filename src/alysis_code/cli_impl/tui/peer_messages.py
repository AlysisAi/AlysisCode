"""Literal, cell-wrapped peer-message blocks for the terminal transcript."""

from __future__ import annotations

import json

from rich.text import Text

from ...surface.console import make_console
from .tool_trace import clean_text, preformatted_rows


def peer_message_rows(entry: str, width: int) -> list[list[tuple[str, str]]]:
    message = json.loads(entry)
    sent = message["direction"] == "sent"
    label = "↗ Sent to" if sent else "↙ Received from"
    peer = clean_text(str(message.get("peer_id") or "unknown chat"))
    state = str(message.get("state") or ("sent" if sent else "received")).capitalize()
    header = f"{label} {peer} · {state}"
    rows = preformatted_rows(header, width, "class:tui.trace.agent")
    rail = [("class:tui.diff.muted", "│ ")] if width > 3 else []
    content = message.get("content")
    body = clean_text(content) if isinstance(content, str) else "Message text is unavailable."
    body_width = max(1, width - (2 if rail else 0))
    # Prose wraps at word boundaries; long URLs, identifiers and scripts
    # without spaces still fold by terminal cells. Text() keeps markup literal.
    wrapped = Text(body).wrap(make_console(width=body_width), body_width, overflow="fold")
    rows.extend(rail + row for line in wrapped for row in preformatted_rows(line.plain, body_width))
    error = message.get("error")
    if isinstance(error, str) and error:
        rows.extend(preformatted_rows(clean_text(error), width, "class:tui.trace.warning"))
    return rows
