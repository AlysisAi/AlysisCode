"""What the compactor knows about a stretch of conversation.

Only facts that are the same in every language: where the stretch is, how
large it is, and which of its messages are on the user's side of the
conversation. Nothing here reads what a message means. What matters in the
text being compacted is decided by the model that summarises it; what the
user's side said is kept by role.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from alysis_code.token_budget import estimate_tokens


@dataclass(frozen=True)
class UserMessage:
    index: int
    text: str


@dataclass(frozen=True)
class TurnSpan:
    start: int
    end: int
    token_estimate: int
    user_messages: tuple[UserMessage, ...] = ()


# Keys the host sets on user-role messages it wrote itself, which are not
# something the user's side said: tool pictures and views and hook lines
# (agent.llm_calls.TOOL_CONTEXT_MESSAGE_KEY), and memory receipts.
HOST_CONTEXT_KEYS = ("alysis_tool_context", "_memory_evidence_context")
# Set on a note the host writes when a turn begins
# (agent.llm_calls.TURN_SETUP_MESSAGE_KEY).
TURN_SETUP_KEY = "alysis_turn_setup"
# The marks above that no provider is ever sent
# (agent.llm_calls.HOST_MESSAGE_KEYS).
HOST_MESSAGE_KEYS = ("alysis_tool_context", TURN_SETUP_KEY)
# How the desktop app heads a file it attaches to a message.
_ATTACHED_ITEM_HEADING = "[Attached item]"


def is_user_message(msg: dict[str, Any]) -> bool:
    """Whether the conversation attributes this message to the user's side.

    Role is the fact used, less the messages the host marks as its own.
    """
    return str(msg.get("role") or "") == "user" and not any(
        msg.get(key) for key in HOST_CONTEXT_KEYS
    )


def user_side_text(msg: dict[str, Any]) -> str:
    """What the user's side typed in a message, or "" when it typed nothing.

    The desktop app wraps the typed text in its own context and marks where
    the typed text begins; only that text is returned, and nothing when the
    wrapper holds no typed text. Any other message is returned as it is. A
    message that holds only images has no text.
    """
    if not is_user_message(msg):
        return ""
    content = msg.get("content")
    if isinstance(content, list):
        content = "\n".join(
            text
            for part in content
            if isinstance(part, dict)
            and part.get("type") in {"text", "input_text"}
            and (text := str(part.get("text") or ""))
        )
    if not isinstance(content, str) or not content.strip():
        return ""
    from alysis_code.agent.prompt_context import (
        DESKTOP_CONTEXT_PREFIXES,
        extract_user_visible_instruction,
    )

    if not content.lstrip().startswith(DESKTOP_CONTEXT_PREFIXES):
        return content
    typed = extract_user_visible_instruction(content)
    if typed.startswith((*DESKTOP_CONTEXT_PREFIXES, _ATTACHED_ITEM_HEADING)):
        return ""  # still the app's own material: no typed text was told apart
    return typed


def estimate_turn_tokens(turn_messages: list[dict[str, Any]]) -> int:
    from alysis_code.compaction.conversation_compactor import (
        sanitize_messages_for_estimation,
    )

    payload = json.dumps(
        sanitize_messages_for_estimation(turn_messages),
        ensure_ascii=False,
        sort_keys=True,
    )
    return max(1, estimate_tokens(payload))
