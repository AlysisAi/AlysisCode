"""Turn-owned cancellation for MCP requests, never serialized as tool arguments."""

import json
from collections.abc import Collection
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from ..cancellation import raise_if_cancelled

_CURRENT: ContextVar[Any | None] = ContextVar("mcp_cancellation_token", default=None)


def current_mcp_cancellation_token() -> Any | None:
    return _CURRENT.get()


@contextmanager
def mcp_cancellation_scope(token: Any | None):
    raise_if_cancelled(token)
    marker = _CURRENT.set(token)
    try:
        yield
        raise_if_cancelled(token)
    finally:
        _CURRENT.reset(marker)


def complete_interrupted_mcp_batch(
    messages: list[dict[str, Any]], *, mcp_tool_names: Collection[str]
) -> list[dict[str, Any]]:
    """Close an interrupted MCP batch without claiming that its effects rolled back.

    A provider requires a result for every emitted call, including siblings that
    cancellation prevented us from starting. Only the current turn's last batch
    is eligible; completed results remain authoritative and are never replaced.
    Returned receipts are safe to persist and contain no arguments or server data.
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "user":
            return []
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        calls = [call for call in message["tool_calls"] if isinstance(call, dict)]
        if not any((call.get("function") or {}).get("name") in mcp_tool_names for call in calls):
            return []
        position = index + 1
        answered: set[str] = set()
        while position < len(messages) and messages[position].get("role") == "tool":
            answered.add(str(messages[position].get("tool_call_id") or ""))
            position += 1
        receipts: list[dict[str, Any]] = []
        for call in calls:
            call_id = str(call.get("id") or "")
            if not call_id or call_id in answered:
                continue
            result = {
                "status": "interrupted",
                "error_code": "tool_call_interrupted",
                "message": (
                    "The turn was interrupted before this tool's result was recorded. "
                    "Its outcome is unconfirmed. Check the current state before retrying; "
                    "do not assume that any effects were rolled back."
                ),
            }
            content = json.dumps(result, separators=(",", ":"))
            messages.insert(position, {"role": "tool", "tool_call_id": call_id, "content": content})
            position += 1
            answered.add(call_id)
            receipts.append(
                {
                    "name": str((call.get("function") or {}).get("name") or ""),
                    "tool_call_id": call_id,
                    "result": result,
                    "content": content,
                }
            )
        return receipts
    return []
