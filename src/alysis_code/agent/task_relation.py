"""Per-prompt objective intent; never a restored objective or workspace reset."""

from __future__ import annotations

from typing import Any

from ..runtime_kind import RuntimeKind
from .prompt_context import extract_user_visible_instruction


def new_task_instruction(
    session: Any, instruction: str, task_relation: str | None, *, chat_only: bool = False
) -> str | None:
    """Validate before effects and resolve only the original trusted user boundary.

    None is the internal omitted-option representation. Wire/queue callers must
    distinguish an absent member from a present null before calling this helper.
    Pinned or delegated callers have a different acceptance authority and are
    deliberately unsupported, including when their text happens to compare equal.
    """
    if task_relation is None:
        return None
    if type(task_relation) is not str or task_relation != "new_task":
        raise ValueError("task_relation must be new_task when present")
    if (
        getattr(session, "runtime_kind", None) != RuntimeKind.INTERACTIVE_CHAT
        or getattr(session, "sensitive_media", False)
        or chat_only
    ):
        raise ValueError("task_relation requires ordinary interactive chat")
    if (
        getattr(session, "acceptance_instruction", None) is not None
        or getattr(session, "subagent_depth", 0) != 0
    ):
        raise ValueError("task_relation cannot replace pinned or delegated acceptance authority")
    accepted = extract_user_visible_instruction(instruction)
    if not accepted:
        raise ValueError("task_relation requires a current user instruction")
    return accepted
