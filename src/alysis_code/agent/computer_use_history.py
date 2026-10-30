"""Keep only the latest Computer Use screen state complete in replayed history.

Legacy Windows Computer Use runs as ordinary MCP tools, so each computer_state
and computer_action result (snapshot envelope plus element lines) stays in the
transcript and is replayed on every later model call. Only the newest state is
actionable: a snapshot_id is valid for one action and the screen has moved on.

When a newer complete state arrives, each older observation is rewritten once,
in place, to a short stub. The tool_call/tool_result pairing, every host-added
field (tool identity, error flag, content summary, visual-delivery outcome and
error) and any action receipt are kept; only the stale state text is dropped.
Error results, results without a parseable state and results of the current
step are never touched. A stubbed message is never rewritten again, so the
replayed prefix changes at most at one boundary per new observation. (Durable
tool-result events keep the full original; a resumed session re-stubs from them.)

The macOS host tools need none of this: their results are already exposed to a
single provider request and then replaced by a redaction stub
(``sensitive_output.redact_consumed_sensitive_tool_messages``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from ..mcp.untrusted_content import build_untrusted_mcp_text_block

COMPUTER_USE_OBSERVATION_METADATA_KEY = "computer_use_observation"
SUPERSEDED_OBSERVATION_NOTE = "[earlier screen state omitted; use the latest state]"
_SUPERSEDED_MARKER_KEY = "superseded_observation"
_SUPERSEDED_MARKER = f'"{_SUPERSEDED_MARKER_KEY}":true'
_BODY_BEGIN = "--- BEGIN UNTRUSTED MCP TEXT ---\n"
_BODY_END = "\n--- END UNTRUSTED MCP TEXT ---"
# Outer payload keys that hold the server's screen data; every other outer key was added
# by the host (tool identity, error flag, content summary, visual-delivery outcome and
# error, delivery id, image count) and records what actually happened, so it is kept.
_STATE_PAYLOAD_KEYS = frozenset({"content"})


def _unwrap_untrusted_text(text: str) -> str | None:
    start = text.find(_BODY_BEGIN)
    end = text.rfind(_BODY_END)
    if start < 0 or end < start + len(_BODY_BEGIN):
        return None
    return text[start + len(_BODY_BEGIN) : end]


def _mime_type(text: str) -> str | None:
    for line in text.splitlines()[:8]:
        if line.startswith("mime_type: "):
            value = line.removeprefix("mime_type: ").strip()
            return None if value in {"", "unknown"} else value
    return None


def _parse_observation(content: Any) -> tuple[dict[str, Any], dict[str, Any], str] | None:
    """Return (payload, body, wrapped_text) for a successful single-text observation."""

    if not isinstance(content, str) or _SUPERSEDED_MARKER in content:
        return None
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("is_error") is not False:
        return None
    if payload.get("offloaded") or "error" in payload or "structured_content" in payload:
        # Structured content may carry its own receipt; keep such results whole.
        return None
    items = payload.get("content")
    if not isinstance(items, list):
        return None
    texts = [item for item in items if isinstance(item, dict) and item.get("type") == "text"]
    if len(texts) != 1 or not isinstance(texts[0].get("text"), str):
        return None
    wrapped = texts[0]["text"]
    raw_body = _unwrap_untrusted_text(wrapped)
    if raw_body is None:
        return None
    try:
        body = json.loads(raw_body)
    except (TypeError, ValueError):
        # Truncated or non-JSON text: keep it whole rather than risk dropping a receipt.
        return None
    if not isinstance(body, dict) or "error" in body:
        return None
    return payload, body, wrapped


def _state_of(body: dict[str, Any]) -> dict[str, Any] | None:
    state = body.get("state") if "receipt" in body else body
    if not isinstance(state, dict) or not isinstance(state.get("snapshot_id"), str):
        return None
    if "elements" not in state:
        return None
    return state


def observation_snapshot_id(content: Any) -> str | None:
    """The snapshot_id of a complete observation in a tool message, else None."""

    parsed = _parse_observation(content)
    if parsed is None:
        return None
    state = _state_of(parsed[1])
    return None if state is None else str(state["snapshot_id"])


def superseded_observation_content(content: Any) -> str | None:
    """The stub for one stale complete observation, or None to keep it as is."""

    parsed = _parse_observation(content)
    if parsed is None:
        return None
    payload, body, wrapped = parsed
    state = _state_of(body)
    if state is None:
        return None
    stub: dict[str, Any] = {
        key: value for key, value in payload.items() if key not in _STATE_PAYLOAD_KEYS
    }
    if "receipt" in body:
        # The receipt says what was done and how; it stays verbatim, and as
        # server-authored text it stays fenced as untrusted.
        text = json.dumps(
            {"receipt": body["receipt"], "state": SUPERSEDED_OBSERVATION_NOTE},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        stub_text = build_untrusted_mcp_text_block(
            source_type="tool_result",
            server_id=str(payload.get("server_id") or ""),
            source_name=str(payload.get("tool_name") or ""),
            text=text,
            mime_type=_mime_type(wrapped),
        )
    else:
        # A bare state has nothing worth keeping; the note is host-authored.
        stub_text = SUPERSEDED_OBSERVATION_NOTE
    # Only the state text item is replaced. Image items hold no pixels here, only the
    # host's delivery summary ("image delivered separately" / "image omitted"), so they stay.
    stub["content"] = [
        {"type": "text", "text": stub_text}
        if isinstance(item, dict) and item.get("type") == "text"
        else item
        for item in payload["content"]
    ]
    stub[_SUPERSEDED_MARKER_KEY] = True
    return json.dumps(stub, ensure_ascii=False, separators=(",", ":"))


def _observation_call_ids(
    messages: list[dict[str, Any]], observation_tool_names: frozenset[str]
) -> set[str]:
    call_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or ():
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            name = function.get("name") if isinstance(function, dict) else call.get("name")
            call_id = str(call.get("id") or "")
            if call_id and name in observation_tool_names:
                call_ids.add(call_id)
    return call_ids


def supersede_computer_use_observations(
    messages: list[dict[str, Any]],
    *,
    latest_tool_call_id: str,
    observation_tool_names: Iterable[str],
    current_step_call_ids: Iterable[str] = (),
) -> int:
    """Stub older complete observations once the latest one carries a new state.

    Mutates ``messages`` in place and returns how many tool results were stubbed.
    Results of the current step are kept: the model has not seen them yet.
    A page of the same snapshot (page_cursor) does not supersede earlier pages.
    """

    names = frozenset(str(name) for name in observation_tool_names if name)
    if not names or not latest_tool_call_id:
        return 0
    latest_content = next(
        (
            message.get("content")
            for message in reversed(messages)
            if isinstance(message, dict)
            and message.get("role") == "tool"
            and message.get("tool_call_id") == latest_tool_call_id
        ),
        None,
    )
    latest_snapshot = observation_snapshot_id(latest_content)
    if latest_snapshot is None:
        return 0
    keep = {str(call_id) for call_id in current_step_call_ids} | {latest_tool_call_id}
    candidates = _observation_call_ids(messages, names) - keep
    stubbed = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        if str(message.get("tool_call_id") or "") not in candidates:
            continue
        content = message.get("content")
        if observation_snapshot_id(content) in (None, latest_snapshot):
            continue
        replacement = superseded_observation_content(content)
        if replacement is None:
            continue
        message["content"] = replacement
        stubbed += 1
    return stubbed


__all__ = [
    "COMPUTER_USE_OBSERVATION_METADATA_KEY",
    "SUPERSEDED_OBSERVATION_NOTE",
    "observation_snapshot_id",
    "superseded_observation_content",
    "supersede_computer_use_observations",
]
