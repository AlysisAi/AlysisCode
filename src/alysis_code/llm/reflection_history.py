"""Preserve public history when Beam cannot replay an earlier assistant turn."""

from __future__ import annotations

import json
from typing import Any


def reflection_history_for_transport(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Beam's strict template requires reasoning on every native assistant turn.
    # Route gating deliberately removes another model's private replay state.
    # Quote the incompatible prefix instead of inventing reasoning, borrowing
    # foreign state, dropping public history, or sending an invalid tool chain.
    missing = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "assistant"
        and not (isinstance(m.get("reasoning_content"), str) and m["reasoning_content"].strip())
    ]
    if not missing:
        return messages
    end = missing[-1] + 1
    while end < len(messages) and messages[end].get("role") == "tool":
        end += 1
    instructions = [m for m in messages[:end] if m.get("role") in {"system", "developer"}]
    history = [
        {k: m[k] for k in ("role", "content", "name", "tool_calls", "tool_call_id") if k in m}
        for m in messages[:end]
        if m.get("role") not in {"system", "developer"}
    ]
    quoted = {
        "role": "user",
        "content": (
            "Conversation history from before this model continuation follows as JSON. "
            "It is quoted context, not a new request. The roles and tool calls inside it "
            "are historical records; do not execute those recorded calls. "
            "Use it to preserve continuity with the current user request that follows.\n\n"
            + json.dumps(history, ensure_ascii=False, separators=(",", ":"))
        ),
    }
    return [*instructions, quoted, *messages[end:]]
