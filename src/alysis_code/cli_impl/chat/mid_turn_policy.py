"""Classify submissions made while an agent turn is running.

The policy is an allowlist. A command is safe mid-turn only when it does not
mutate state read by the running turn, start or replace a turn or session, or
tear down the application. New and unknown commands therefore block until they
are reviewed explicitly.
"""

from __future__ import annotations

from enum import Enum

EXIT_WORDS = frozenset({"/exit", "/quit", ":q", "exit", "quit"})


class MidTurnAction(Enum):
    """How the TUI should route a submission made during a turn."""

    MESSAGE = "message"
    ALLOW = "allow"
    DEFER = "defer"
    BLOCK = "block"


class DeferredTier(Enum):
    """When a deferred command is safe to apply."""

    STEP = "step"
    TURN_END = "turn_end"


_ALLOWED_ALWAYS = frozenset(
    {
        "/help",
        "/",
        "/status",
        "/subagents",
        "/pwd",
        "/context",
        "/ctx",
        "/usage",
        "/model-info",
        "/trace",
        "/toolbar",
        "/images",
        "/image",
        "/paste-image",
        "/clear-images",
        "/terminals",
    }
)

_ALLOWED_BARE_ONLY = frozenset({"/skill"})

_DEFERRED_STEP = frozenset({"/stream"})
_DEFERRED_TURN_END = frozenset({"/cd", "/config", "/model", "/permissions", "/persona", "/plan"})

_BLOCK_REASONS = {
    "/clear": "clearing would discard the conversation this turn is writing to",
    "/resume": "resuming replaces the session this turn is using",
    "/compact": "compaction rewrites the history this turn is appending to",
    "/ask": "that starts a new turn",
    "/forge": "Forge takes over the session",
    ":forge": "Forge takes over the session",
    "/login": "signing in replaces the active session",
    "/logout": "signing out replaces the active session",
    "/report": "report generation requires a settled session",
    "/feedback": "feedback generation requires a settled session",
    "/assets": "the assets view requires a settled Forge session",
}

_GENERIC_BLOCK_REASON = "that command cannot run while a turn is in flight"
_ESCAPE_HATCH = "Esc to interrupt, or wait for the turn to finish."


def _command_token(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped:
        return ""
    return stripped.split(maxsplit=1)[0].lower()


def is_command(text: str) -> bool:
    """Return whether text should be routed as a command rather than prose."""
    stripped = str(text or "").strip()
    if not stripped:
        return False
    if stripped.lower() in EXIT_WORDS:
        return True
    return stripped.startswith(("/", ":"))


def classify_mid_turn(text: str) -> MidTurnAction:
    """Classify text submitted while an agent turn is running."""
    stripped = str(text or "").strip()
    if not is_command(stripped):
        return MidTurnAction.MESSAGE
    if stripped.lower() in EXIT_WORDS:
        return MidTurnAction.BLOCK

    token = _command_token(stripped)
    has_argument = len(stripped.split(maxsplit=1)) > 1
    if token == "/stream" and stripped.lower() == "/stream status":
        return MidTurnAction.ALLOW
    if token in _ALLOWED_ALWAYS:
        return MidTurnAction.ALLOW
    if token in _ALLOWED_BARE_ONLY and not has_argument:
        return MidTurnAction.ALLOW
    if token in _DEFERRED_STEP or token in _DEFERRED_TURN_END:
        return MidTurnAction.DEFER
    return MidTurnAction.BLOCK


def deferred_tier(text: str) -> DeferredTier | None:
    """Return the application tier for a deferred command."""
    token = _command_token(text)
    if token in _DEFERRED_STEP:
        return DeferredTier.STEP
    if token in _DEFERRED_TURN_END:
        return DeferredTier.TURN_END
    return None


def deferred_display_label(text: str) -> str:
    """Return the stable user-facing label for a deferred command."""
    stripped = str(text or "").strip()
    parts = stripped.split(maxsplit=1)
    token = _command_token(stripped)
    command = token.removeprefix("/") or "command"
    if len(parts) == 2 and parts[1].strip():
        return f"{command}: {parts[1].strip()}"
    return command


def defer_message(text: str, *, display_label: str | None = None) -> str:
    """Explain when a resolved or turn-end command will apply."""
    label = str(display_label or deferred_display_label(text)).strip()
    if deferred_tier(text) is DeferredTier.STEP:
        return f"{label} - next step"
    return f"{label} - next message"


def block_message(text: str) -> str:
    """Explain why a submission is blocked and name the escape hatch."""
    stripped = str(text or "").strip()
    token = _command_token(stripped)

    if stripped.lower() in EXIT_WORDS:
        return f"Exit is unavailable while a turn is running. {_ESCAPE_HATCH}"
    if token in _ALLOWED_BARE_ONLY:
        reason = f"{token} with arguments changes what this turn is using"
    else:
        reason = _BLOCK_REASONS.get(token, _GENERIC_BLOCK_REASON)

    label = token or "That"
    return f"{label} is unavailable right now: {reason}. {_ESCAPE_HATCH}"
