"""Expiring local metadata trace. No UI text, prompt, reasoning or typed content."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any

from .llm.focused_input_diagnostic import diagnostic_root

_ROLES = {
    "AXTextField",
    "AXTextArea",
    "AXComboBox",
    "AXOutline",
    "AXTable",
    "AXList",
    "AXWindow",
    "AXGroup",
}
_ACTIONS = {
    "activate_application",
    "key",
    "type",
    "press",
    "focus",
    "set_value",
    "click",
    "scroll",
    "drag",
}
_APPS = {"Finder", "TextEdit"}
_KEYS = {
    "command",
    "cmd",
    "shift",
    "control",
    "ctrl",
    "alt",
    "option",
    "return",
    "enter",
    "escape",
    "tab",
    "n",
    "s",
    "w",
    "a",
    "c",
    "v",
}


def focus_diagnostic_projection(
    phase: str, tool: str, arguments: dict[str, Any], result: Any, *, origin: str = "model"
) -> dict[str, Any] | None:
    if phase not in {"tool_projection", "decision_result"} or tool not in {
        "computer_state",
        "computer_action",
        "computer_wait",
    }:
        return None
    if not isinstance(result, dict):
        return None
    state = result.get("state") if tool == "computer_action" else result
    if not isinstance(state, dict):
        return None
    target = state.get("target")
    if not isinstance(target, dict) or target.get("bundle_id") not in {
        "com.apple.finder",
        "com.apple.TextEdit",
    }:
        return None
    snapshot = state.get("snapshot_id")
    if not isinstance(snapshot, str) or not re.fullmatch(r"snapshot_[a-f0-9-]{36}", snapshot):
        return None
    focus = state.get("focused_target")
    focus = focus if isinstance(focus, dict) else {}
    elements = state.get("elements")
    elements = [e for e in elements if isinstance(e, dict)] if isinstance(elements, list) else []
    row = {
        "phase": phase,
        "tool": tool,
        "origin": origin if origin in {"model", "controller", "wrapper"} else "unknown",
        "snapshot": snapshot,
        "bundle": target["bundle_id"],
        "element_count": len(elements),
        "window": hashlib.sha256(target["window_id"].encode()).hexdigest()[:16]
        if isinstance(target.get("window_id"), str)
        else "unavailable",
        "truncated": state.get("truncated") is True,
        "available": focus.get("available") is True,
        "key": focus.get("key") is True,
        "type": focus.get("type") is True,
        "role": focus.get("role") if focus.get("role") in _ROLES else "other_or_unavailable",
        "focused_roles": [
            e.get("role") if e.get("role") in _ROLES else "other"
            for e in elements
            if e.get("focused") is True
        ][:8],
        "action": arguments.get("action") if arguments.get("action") in _ACTIONS else "none",
        "application": arguments.get("application")
        if arguments.get("application") in _APPS
        else "other_or_none",
        "input_target": "focused"
        if arguments.get("target") == "focused"
        else "element"
        if "element_id" in arguments
        else "none",
        "image_requested": arguments.get("include_image") is True,
    }
    keys = arguments.get("keys")
    if isinstance(keys, list):
        row["keys"] = [
            k.lower() if isinstance(k, str) and k.lower() in _KEYS else "other" for k in keys[:8]
        ]
    return row


def record_focus_diagnostic(
    phase: str, tool: str, arguments: dict[str, Any], result: Any, *, origin: str = "model"
) -> None:
    try:
        root = diagnostic_root()
        until = float((root / "enabled-until.txt").read_text())
        now = time.time()
        if not now < until <= now + 7200:
            return
        row = focus_diagnostic_projection(phase, tool, arguments, result, origin=origin)
        if row is None:
            return
        fd = os.open(
            root / "engine.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
        try:
            os.fchmod(fd, 0o600)
            if os.fstat(fd).st_size < 1_048_576:
                os.write(fd, (json.dumps({"at": now, **row}) + "\n").encode())
        finally:
            os.close(fd)
    except Exception:
        pass
