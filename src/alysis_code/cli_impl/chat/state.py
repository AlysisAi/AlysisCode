from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _ChatExecutionRequest:
    instruction: str
    routing_mode_override: str | None = None
    ephemeral_system_messages: tuple[str, ...] = ()
    ephemeral_user_messages: tuple[str, ...] = ()
    # One-turn execution-mode override (``/ask``): the chat loop applies
    # ``mode_override`` before the turn and restores ``restore_mode_after``
    # afterwards, exception-safe.
    mode_override: str | None = None
    restore_mode_after: str | None = None
    chat_only: bool = False


__all__ = [name for name in globals() if not name.startswith("__") or name == "__version__"]
