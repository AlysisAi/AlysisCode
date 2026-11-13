"""Where a bot's request keeps the state of the task it is working on.

The first message of a bot's request is the same for every task of the bot.
What belongs to one task (its time, approvals, saved memory, notes, files and
other work in progress) is in a message of its own after the earlier
conversation, just before the task's request.
"""

from __future__ import annotations

from typing import Any

from alysis_code.bots.runtime import TASK_STATE_HEADING


def task_state(messages: list[dict[str, Any]]) -> str:
    """The text of the message that carries the task's own state."""
    return next(
        message["content"]
        for message in messages
        if isinstance(message.get("content"), str)
        and message["content"].startswith(TASK_STATE_HEADING)
    )
