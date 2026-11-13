"""Thread-safe handoff for mid-turn user steering.

A running turn owns ``session.messages`` on its worker thread. The TUI thread
must not append there directly: compaction can replace the list object, and a
cross-thread append can also split an assistant tool call from its tool result.
This module gives the UI and worker threads a narrow, lock-guarded mailbox.

The design mirrors the parent-message inbox used by background subagents, with
one deliberate difference. Child steering is an ephemeral ``system`` prompt
for one request. A user steering their own turn becomes a durable ``user``
message because their words belong in conversation history, must survive later
steps, and must persist across resume.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import Event, RLock
from typing import Any

MAX_STEER_MESSAGE_CHARS = 4000
MAX_PENDING_STEER_MESSAGES = 16
MAX_PENDING_OPS = 16
_TRUNCATION_MARKER = " [truncated]"


@dataclass(frozen=True)
class ResolvedOperation:
    """A validated operation that is safe for worker-thread application."""

    kind: str
    payload: Any
    display_label: str


class OpsInbox:
    """Bounded, thread-safe FIFO for resolved mid-turn operations."""

    __slots__ = ("_lock", "_operations")

    def __init__(self) -> None:
        self._lock = RLock()
        self._operations: deque[ResolvedOperation] = deque()

    def send(self, operation: ResolvedOperation) -> bool:
        """Queue ``operation`` or return False when the inbox is full."""
        if not isinstance(operation, ResolvedOperation):
            raise TypeError("operation must be a ResolvedOperation")
        with self._lock:
            if len(self._operations) >= MAX_PENDING_OPS:
                return False
            self._operations.append(operation)
            return True

    def drain(self) -> list[ResolvedOperation]:
        """Remove and return all pending operations, oldest first."""
        with self._lock:
            if not self._operations:
                return []
            operations = list(self._operations)
            self._operations.clear()
            return operations

    def snapshot(self) -> list[ResolvedOperation]:
        """Return pending operations in FIFO order without removing them."""
        with self._lock:
            return list(self._operations)

    def pending_count(self) -> int:
        """Return the number of operations waiting for application."""
        with self._lock:
            return len(self._operations)


def _normalize_steer_message(message: str) -> str:
    normalized = str(message or "").strip()
    if not normalized:
        return ""
    if len(normalized) > MAX_STEER_MESSAGE_CHARS:
        prefix_limit = MAX_STEER_MESSAGE_CHARS - len(_TRUNCATION_MARKER)
        normalized = normalized[:prefix_limit].rstrip() + _TRUNCATION_MARKER
    return normalized


class SteerInbox:
    """Bounded, thread-safe, drain-once mailbox for steering messages."""

    __slots__ = (
        "_dropped",
        "_lock",
        "_messages",
        "_wake_event",
        "_wake_signals",
    )

    def __init__(self) -> None:
        self._lock = RLock()
        self._messages: deque[str] = deque()
        self._wake_event = Event()
        self._wake_signals: deque[dict[str, str]] = deque()
        self._dropped = 0

    @property
    def wake_event(self) -> Event:
        """Notification checked by parent waits that are already polling."""
        return self._wake_event

    def _signal_locked(self, *, reason: str, run_id: str = "") -> None:
        signal = {"reason": str(reason or "parent_wake").strip() or "parent_wake"}
        if run_id:
            signal["run_id"] = str(run_id)
        self._wake_signals.append(signal)
        while len(self._wake_signals) > MAX_PENDING_STEER_MESSAGES:
            self._wake_signals.popleft()
        self._wake_event.set()

    def signal_waiters(self, *, reason: str, run_id: str = "") -> None:
        """Wake a parent wait without adding user-visible conversation text."""
        with self._lock:
            self._signal_locked(reason=reason, run_id=run_id)

    def consume_wait_signal(self) -> dict[str, str] | None:
        """Consume one wake reason after a polling wait observes the event."""
        with self._lock:
            if not self._wake_signals:
                self._wake_event.clear()
                return None
            signal = dict(self._wake_signals.popleft())
            if not self._wake_signals:
                self._wake_event.clear()
            return signal

    def consume_wait_signals(self) -> list[dict[str, str]]:
        """Atomically drain the wake reasons present when processing begins."""
        with self._lock:
            if not self._wake_signals:
                self._wake_event.clear()
                return []
            signals = [dict(signal) for signal in self._wake_signals]
            self._wake_signals.clear()
            self._wake_event.clear()
            return signals

    def send(self, message: str) -> str:
        """Queue a normalized message and return the text actually queued."""
        normalized = _normalize_steer_message(message)
        if not normalized:
            return ""
        with self._lock:
            self._messages.append(normalized)
            while len(self._messages) > MAX_PENDING_STEER_MESSAGES:
                self._messages.popleft()
                self._dropped += 1
            self._signal_locked(reason="parent_steer")
        return normalized

    def restore_front(self, messages: list[str]) -> None:
        """Restore older drained messages ahead of any newer arrivals atomically."""
        normalized = [clean for message in messages if (clean := _normalize_steer_message(message))]
        if not normalized:
            return
        with self._lock:
            self._messages.extendleft(reversed(normalized))
            while len(self._messages) > MAX_PENDING_STEER_MESSAGES:
                self._messages.popleft()
                self._dropped += 1
            self._signal_locked(reason="parent_steer")

    def drain(self) -> list[str]:
        """Remove and return all pending messages, oldest first."""
        with self._lock:
            if not self._messages:
                return []
            messages = list(self._messages)
            self._messages.clear()
            self._wake_signals = deque(
                signal for signal in self._wake_signals if signal.get("reason") != "parent_steer"
            )
            if not self._wake_signals:
                self._wake_event.clear()
            return messages

    def pending_count(self) -> int:
        """Return the number of messages waiting for delivery."""
        with self._lock:
            return len(self._messages)

    def dropped_count(self) -> int:
        """Return the number of messages evicted by the queue bound."""
        with self._lock:
            return self._dropped


def wait_signal_digest(signals: list[dict[str, str]]) -> dict[str, object]:
    """Build the compatible first-reason fields plus the complete wake digest."""
    normalized = [
        {
            "reason": str(signal.get("reason") or "parent_wake"),
            **({"run_id": str(signal["run_id"])} if str(signal.get("run_id") or "") else {}),
        }
        for signal in signals
    ]
    if not normalized:
        return {}
    first = normalized[0]
    return {
        "wake_reason": first["reason"],
        "wake_reasons": normalized,
        **({"wake_run_id": first["run_id"]} if first.get("run_id") else {}),
    }


def ops_inbox_for(session: object, *, create: bool = False) -> OpsInbox | None:
    """Return a session's ops inbox, optionally attaching one as an attribute."""
    existing = getattr(session, "ops_inbox", None)
    if isinstance(existing, OpsInbox):
        return existing
    if not create:
        return None
    inbox = OpsInbox()
    try:
        session.ops_inbox = inbox  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - frozen or slotted sessions opt out
        return None
    return inbox


def steer_inbox_for(session: object, *, create: bool = False) -> SteerInbox | None:
    """Return a session's inbox, optionally attaching one as a plain attribute."""
    existing = getattr(session, "steer_inbox", None)
    if isinstance(existing, SteerInbox):
        return existing
    if not create:
        return None
    inbox = SteerInbox()
    try:
        session.steer_inbox = inbox  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - frozen or slotted sessions opt out
        return None
    return inbox


# How the host writes into the conversation what the user's side adds to a turn
# that is already running. Both continue the request being worked on; neither
# starts a new one.
MID_TURN_USER_MESSAGE_PREFIX = "[Mid-turn message from the user] "
USER_QUESTION_RESPONSE_PREFIX = "[User question response] "
TURN_CONTINUATION_PREFIXES = (MID_TURN_USER_MESSAGE_PREFIX, USER_QUESTION_RESPONSE_PREFIX)


def build_steer_messages(messages: list[str]) -> list[dict[str, str]]:
    """Build durable user-history messages from drained steering text."""
    built: list[dict[str, str]] = []
    for message in messages:
        text = str(message or "").strip()
        if not text:
            continue
        built.append(
            {
                "role": "user",
                "content": f"{MID_TURN_USER_MESSAGE_PREFIX}{text}",
            }
        )
    return built
