"""What an owl's model reads of its computer's focused window: the element view.

The computer's desktop reads the focused window's accessibility tree (desktop_accessibility.py,
inside the computer); this module turns that reading into text, one line per meaningful element,
in the browser page view's shape (ide/browser_observation.py accessibility_page_view):
``- role "name" [states] [ref=eN]: text or value``. Unnamed containers are flattened; a password
field shows only ``[private field]``. Refs name elements, not readings: an element keeps its ref
in every later reading while it lives, and a ref never names another element. The tree's roles
and states are AT-SPI vocabulary, the same for every toolkit and language; names, text and
values are the application's, untrusted, shown as they are.
"""

from __future__ import annotations

import json
import math
from typing import Any

from ..ide.browser_observation import _PRIVATE_FIELD, _clip, _squash
from .desktop_accessibility import (
    NAME_CHARS,
    PRIVATE_ROLES,
    TEXT_CHARS,
    TOGGLE_ROLES,
    element_identity,
)
from .desktop_linux import DesktopError

ELEMENT_VIEW_CHARS = 16_000
MAX_REFS = 4000
_INDENT_LEVELS = 20
_ROLE_CHARS = 60
_WINDOW_LINES = 20

# Containers without meaning of their own: unnamed, their content is shown in their place.
_WRAPPER_ROLES = frozenset(
    {
        "filler",
        "glass pane",
        "grouping",
        "layered pane",
        "panel",
        "redundant object",
        "root pane",
        "scroll pane",
        "section",
        "split pane",
        "unknown",
        "viewport",
    }
)
# Elements that are their text: shown as ``- label: text``.
_TEXT_ROLES = frozenset({"caption", "label", "paragraph", "static", "text"})
_FIELD_ROLES = frozenset({"combo box", "entry", "password text", "spin button", "text"})
_ACTING_STATES = frozenset({"checkable", "editable", "focusable", "selectable"})
_ACTING_INTERFACES = frozenset({"Action", "EditableText"})
_STOPPED = {
    "elements": "this window has more elements than one reading holds",
    "calls": "this window has more elements than one reading holds",
    "time": "reading the window took too long",
    "size": "the reading was too large",
    "bus": "the accessibility connection closed",
}
UNAVAILABLE = {
    "unsupported": "this computer cannot read window elements",
    "timeout": "reading the window's elements took too long",
    "bus": "the computer's accessibility service did not answer",
}


class ElementRefusal(DesktopError):
    """An authored, value-free refusal: nothing was sent to the application."""


REFUSALS = {
    "gone": "That element is no longer on the screen (it closed or was replaced), so nothing "
    "was sent. Observe again and use the element's new ref.",
    "changed": "That element changed since it was observed (it is now another kind of "
    "element), so nothing was sent. Observe again and use the element's new ref.",
    "hidden": "That element is not on the screen now (a menu closed, or it scrolled away), so "
    "nothing was sent. Observe again.",
    "disabled": "That element is disabled, so nothing was sent. Do what enables it first, then "
    "observe again.",
    "unresponsive": "The application did not answer (it may be busy), so nothing was sent. "
    "Observe again before repeating.",
    "no_position": "That element can't be activated through its application and has no "
    "position on the screen, so nothing was sent.",
    "read_only": "That field is read-only, so no text was set.",
    "not_toggle": "That element has no on/off state, so nothing was sent; use activate.",
    "radio": "A radio button is cleared by choosing another option of its group, so nothing "
    "was sent.",
    "unavailable": "The computer could not reach that element's application, so nothing was "
    "sent. Observe again.",
}


WINDOW_REFUSALS = {
    "gone": "That window is no longer open, so nothing was sent. Observe again.",
    "unavailable": "The computer could not bring that window to the front, so nothing was "
    "sent. Observe again.",
}


def new_state() -> dict:
    """A computer's refs: ref -> element or window, (bus, path) or ("window", X id) -> ref, and
    the next number. Elements and the open windows share one numbering."""
    return {"refs": {}, "known": {}, "next": 1}


def refusal(code: str) -> ElementRefusal:
    return ElementRefusal(REFUSALS.get(code, REFUSALS["unavailable"]))


def window_refusal(code: str) -> ElementRefusal:
    return ElementRefusal(WINDOW_REFUSALS.get(code, WINDOW_REFUSALS["unavailable"]))


def target(state: dict, ref: int) -> dict:
    """The element a ref names, as the computer addresses it; refused for an unknown ref."""
    element = state["refs"].get(ref)
    if element is None:
        raise ElementRefusal(
            f"e{ref} is not a ref of this computer's element view, so nothing was sent. Use the "
            "number of an element's [ref=eN] from a current observation."
        )
    if "window_id" in element:
        raise ElementRefusal(
            f"e{ref} is a window of other_windows, so nothing was sent. Bring it to the front "
            "with computer_focus_window, or use an element's ref."
        )
    return element


def window_target(state: dict, ref: int) -> dict:
    """The open window a ref of other_windows names; refused for any other ref."""
    window = state["refs"].get(ref)
    if window is None or "window_id" not in window:
        raise ElementRefusal(
            f"e{ref} is not a window of other_windows, so nothing was sent. Use the number of a "
            "window's [ref=eN] from a current observation's other_windows."
        )
    return window


def _key(entry: dict) -> tuple:
    if "window_id" in entry:
        return ("window", entry["window_id"])
    return (entry["bus"], entry["path"])


def windows_of(raw) -> list[dict] | None:
    """The computer's open windows (untrusted data), checked and clipped; None without a list."""
    if not isinstance(raw, list):
        return None
    windows = []
    for item in raw[: _WINDOW_LINES + 10]:
        if not isinstance(item, dict):
            continue
        identity = item.get("id")
        if type(identity) is not int or not 0 < identity <= 0xFFFFFFFF:
            continue
        windows.append(
            {
                "id": identity,
                "title": _text(item.get("title"), NAME_CHARS),
                "class": _text(item.get("class"), NAME_CHARS),
                "active": item.get("active") is True,
            }
        )
    return windows


def window_view(windows: list[dict], state: dict) -> dict:
    """``focused_window`` and ``other_windows`` from the open windows alone (no element reading)."""
    lines, shown, active = window_lines(windows, state)
    _forget(state, shown)
    return {
        **({"focused_window": active} if active else {}),
        **({"other_windows": lines} if lines else {}),
    }


def _window_ref(state: dict, window: dict) -> tuple[int, str]:
    """A ref for an open window, kept while the window lives, and its line."""
    key = ("window", window["id"])
    ref = state["known"].get(key)
    if ref is None or "window_id" not in state["refs"].get(ref, {}):
        ref = state["next"]
        state["next"] += 1
        state["known"][key] = ref
    line = _window({"role": "window", "title": window["title"], "app": window["class"]})
    state["refs"][ref] = {
        "window_id": window["id"],
        "role": "window",
        "name": window["title"],
        "window": line,
    }
    return ref, line


def window_lines(windows: list[dict], state: dict) -> tuple[list[str], set[int], str | None]:
    """other_windows from the computer's open windows: every one but the active one, each with
    a ref for computer_focus_window; the refs shown; and the active window's line."""
    lines: list[str] = []
    shown: set[int] = set()
    active = None
    for window in windows:
        if window["active"]:
            active = _window({"role": "window", "title": window["title"], "app": window["class"]})
            continue
        if len(lines) >= _WINDOW_LINES:
            continue
        ref, line = _window_ref(state, window)
        shown.add(ref)
        lines.append(f"{line} [ref=e{ref}]")
    return lines, shown, active


def _number(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _text(value, limit: int) -> str:
    return _clip(" ".join(value.split()), limit) if isinstance(value, str) else ""


def _strings(value) -> set[str]:
    return {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()


def _states(role: str, states: set[str], acting: bool) -> list[str]:
    shown = []
    if "checked" in states or "pressed" in states:
        shown.append(
            ("checked" if "checked" in states else "pressed")
            + ("=mixed" if "indeterminate" in states else "")
        )
    for state, label in (("selected", "selected"), ("focused", "focused")):
        if state in states:
            shown.append(label)
    if "expandable" in states:
        shown.append("expanded" if "expanded" in states else "collapsed")
    if acting and not states & {"enabled", "sensitive"}:
        shown.append("disabled")
    for state, label in (
        ("read only", "read only"),
        ("required", "required"),
        ("invalid entry", "invalid"),
        ("is default", "default"),
        ("modal", "modal"),
    ):
        if state in states:
            shown.append(label)
    if "editable" in states and role not in _FIELD_ROLES:
        shown.append("editable")
    return shown


def _node(raw) -> dict | None:
    """One element of the computer's reading, checked and clipped (it is untrusted data)."""
    if not isinstance(raw, dict) or type(raw.get("depth")) is not int or raw["depth"] < 0:
        return None
    identity = raw.get("id")
    try:
        bus, path = element_identity(
            {"bus": identity[0], "path": identity[1]} if isinstance(identity, list) else None
        )
    except (ValueError, IndexError):
        return None
    role = _text(raw.get("role"), _ROLE_CHARS)
    if not role:
        return None
    return {
        "bus": bus,
        "path": path,
        "depth": raw["depth"],
        "role": role,
        "name": _text(raw.get("name"), NAME_CHARS),
        "description": _text(raw.get("description"), NAME_CHARS),
        "states": _strings(raw.get("states")),
        "interfaces": _strings(raw.get("interfaces")),
        "private": raw.get("private") is True or role in PRIVATE_ROLES,
        "has_text": raw.get("has_text") is True,
        "text": _text(raw.get("text"), TEXT_CHARS + 1),
        "value": _number(raw.get("value")),
        "not_listed": raw.get("children_not_listed")
        if type(raw.get("children_not_listed")) is int
        else 0,
    }


def _acting(node: dict) -> bool:
    """Whether an action can address the element (it has a ref)."""
    return bool(
        node["interfaces"] & _ACTING_INTERFACES
        or node["states"] & _ACTING_STATES
        or node["role"] in TOGGLE_ROLES
    )


def _ref(state: dict, node: dict, window: str) -> int:
    key = (node["bus"], node["path"])
    ref = state["known"].get(key)
    if ref is None or state["refs"].get(ref, {}).get("role") != node["role"]:
        ref = state["next"]
        state["next"] += 1
        state["known"][key] = ref
    state["refs"][ref] = {
        "bus": node["bus"],
        "path": node["path"],
        "role": node["role"],
        "name": node["name"],
        "window": window,
    }
    return ref


def _forget(state: dict, keep: set[int]) -> None:
    """Keep the refs table bounded: the oldest refs not in the latest view go first."""
    excess = len(state["refs"]) - MAX_REFS
    for ref in sorted(state["refs"]):
        if excess <= 0:
            break
        if ref in keep:
            continue
        element = state["refs"].pop(ref)
        state["known"].pop(_key(element), None)
        excess -= 1


def _window(value) -> str:
    if not isinstance(value, dict):
        return ""
    role = _text(value.get("role"), _ROLE_CHARS) or "window"
    title = _text(value.get("title"), NAME_CHARS)
    app = _text(value.get("app"), NAME_CHARS)
    line = role + (" " + json.dumps(title, ensure_ascii=False) if title else "")
    return line + (f" ({app})" if app and _squash(app) not in _squash(title) else "")


def render(
    reading: Any,
    state: dict,
    *,
    max_chars: int = ELEMENT_VIEW_CHARS,
    open_windows: list[dict] | None = None,
) -> dict:
    """The element view of one reading, with refs from (and added to) ``state``.

    Returns ``text`` (the view, cut at a line within ``max_chars`` with a final line saying what
    was left out), ``focused_window`` and ``other_windows`` (lines naming windows), and
    ``complete`` (whether every element of the window is shown). With ``open_windows`` (the
    computer's list of every open window, windows_of), other_windows names those, each with a
    ref; without it, the windows that describe their elements.
    """
    if not isinstance(reading, dict) or not isinstance(reading.get("nodes"), list):
        raise ValueError("Invalid element reading.")
    focused = _window(reading.get("focused")) if reading.get("focused") else ""
    window_refs: set[int] = set()
    active = None
    if open_windows is not None:
        windows, window_refs, active = window_lines(open_windows, state)
    else:
        windows = [line for line in map(_window, reading.get("windows") or []) if line]
        windows = windows[:_WINDOW_LINES]
    nodes = [node for node in map(_node, reading["nodes"]) if node]
    lines: list[str] = []
    stack: list[tuple[int, int]] = []  # (depth in the reading, depth shown)
    shown_refs: set[int] = set()
    for index, node in enumerate(nodes):
        while stack and stack[-1][0] >= node["depth"]:
            stack.pop()
        depth = stack[-1][1] + 1 if stack else 0
        leaf = index + 1 == len(nodes) or nodes[index + 1]["depth"] <= node["depth"]
        acting = _acting(node)
        tail = ""
        if node["private"]:
            tail = _PRIVATE_FIELD if node["has_text"] else ""
        elif node["text"] and _squash(node["text"]) != _squash(node["name"]):
            tail = node["text"]
        elif node["value"] is not None:
            tail = node["value"]
        text_only = not acting and node["role"] in _TEXT_ROLES
        if not acting and not node["name"] and not tail and not node["not_listed"]:
            if leaf or text_only or node["role"] in _WRAPPER_ROLES:
                # Nothing to read or act on: its content (if any) takes its place.
                stack.append((node["depth"], depth - 1))
                continue
        stack.append((node["depth"], depth))
        indent = "  " * min(depth, _INDENT_LEVELS)
        if text_only:
            lines.append(indent + f"- {node['role']}: {tail or node['name']}")
            continue
        head = "- " + node["role"]
        if node["name"]:
            head += " " + json.dumps(node["name"], ensure_ascii=False)
        head += "".join(f" [{label}]" for label in _states(node["role"], node["states"], acting))
        if acting:
            ref = _ref(state, node, focused)
            shown_refs.add(ref)
            head += f" [ref=e{ref}]"
        if tail:
            head += ": " + tail
        if node["description"] and _squash(node["description"]) != _squash(node["name"]):
            head += f" ({node['description']})"
        if node["not_listed"]:
            head += f" ({node['not_listed']} items inside, not listed)"
        lines.append(indent + head)
    _forget(state, shown_refs | window_refs)
    stopped = reading.get("stopped")
    notes = []
    if isinstance(stopped, str):
        notes.append(
            f"[Stopped at {len(reading['nodes'])} elements: "
            f"{_STOPPED.get(stopped, 'the reading ended early')}; the rest of the window is left "
            "out. Act on what is shown, or close what is in front or scroll to the part you "
            "need, then observe again.]"
        )
    if reading.get("unresponsive") is True:
        notes.append("[An application did not answer in time: its elements may be missing.]")
    if not focused:
        lines = [
            "[No focused window describes its elements: the window in front may be a terminal "
            "or a program without accessibility support. other_windows lists the windows that "
            "do; use the screen image, keys or commands for the one in front.]"
            if open_windows is None
            else "[The window in front does not describe its elements: it may be a terminal "
            "or a program without accessibility support. Use the screen image, keys or "
            "commands for it, or bring one of other_windows to the front.]"
        ]
    text, complete = _cut(lines, max_chars)
    if notes:
        text += "\n" + "\n".join(notes)
    return {
        "text": text,
        # The window manager's active window names the one in front when it describes nothing.
        "focused_window": focused or active,
        "other_windows": windows,
        "complete": complete and not notes and bool(focused),
    }


def _cut(lines: list[str], max_chars: int) -> tuple[str, bool]:
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text, True
    cut = text.rfind("\n", 0, max_chars + 1)
    if cut < max_chars // 2:
        cut = max_chars  # one very long line: cut inside it rather than show almost nothing
    shown = text[:cut]
    left = len(lines) - shown.count("\n") - (0 if cut == max_chars else 1)
    return (
        shown
        + f"\n[Stopped at line {len(lines) - left}: {left} more lines of this window are left "
        "out. Act on what is shown, or close what is in front or scroll to the part you need, "
        "then observe again.]",
        False,
    )
