"""Shared cooperative-cancellation primitives.

One token discipline for every long-running execution surface (IDE bridge
jobs, Forge swarm workers): a ``threading.Event`` is the single cancel
signal, a token wraps it for duck-typed checkpoints, and cancellation
surfaces as a dedicated exception type so callers can distinguish a
cooperative stop from a failure.

The agent turn engine (``agent/turn/core.py``) duck-types tokens: it calls
``throw_if_cancelled()`` when available, otherwise checks ``is_cancelled``.
Both behaviors are provided here.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any


class CooperativeCancellationError(Exception):
    """Raised at a cooperative checkpoint after cancellation was requested."""

    def __init__(self, reason: str = "cancelled_by_user") -> None:
        super().__init__(reason)
        self.reason = str(reason or "cancelled_by_user")


class EventCancellationToken:
    """Duck-typed cancellation token backed by a ``threading.Event``."""

    error_class: type[CooperativeCancellationError] = CooperativeCancellationError

    def __init__(self, event: threading.Event) -> None:
        self._event = event

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def throw_if_cancelled(self, reason: str = "cancelled_by_user") -> None:
        if self.is_cancelled:
            raise self.error_class(reason)

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until cancellation or ``timeout`` without polling."""

        return self._event.wait(timeout)

    def wait_cancelled(self, timeout: float | None = None) -> bool:
        """Wait for cancellation without exposing the backing event."""

        return self.wait(timeout)


class CombinedCancellationToken:
    """Observe caller and budget cancellation without replacing either reason.

    Wait-only compatibility lets existing HTTP/tool boundaries subscribe through
    their short-lived observers. This token owns no background worker or timer.
    """

    def __init__(self, *tokens: Any) -> None:
        self._tokens = tuple(token for token in tokens if token is not None)

    @property
    def is_cancelled(self) -> bool:
        return any(bool(getattr(token, "is_cancelled", False)) for token in self._tokens)

    def throw_if_cancelled(self, reason: str = "cancelled_by_user") -> None:
        for token in self._tokens:
            if not bool(getattr(token, "is_cancelled", False)):
                continue
            throw = getattr(token, "throw_if_cancelled", None)
            if callable(throw):
                throw(reason)
            raise CooperativeCancellationError(getattr(token, "reason", reason))

    def wait(self, timeout: float | None = None) -> bool:
        until = None if timeout is None else time.monotonic() + max(0.0, timeout)
        pause = threading.Event()
        while not self.is_cancelled:
            remaining = None if until is None else until - time.monotonic()
            if remaining is not None and remaining <= 0:
                return self.is_cancelled
            pause.wait(0.05 if remaining is None else min(0.05, remaining))
        return True


class InteractiveCancellationToken:
    """Thread-safe cancellation token for an interactive user turn.

    Interactive callers preserve the established ``KeyboardInterrupt`` control
    flow while still giving providers and nested workers a cooperative token.
    A provider may register an abort callback for an in-flight response; a
    callback installed after cancellation is invoked immediately so the initial
    no-byte wait cannot miss an Esc/Ctrl-C race.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._abort_lock = threading.Lock()
        self._request_callback: object | None = None
        self._abort: object | None = None
        self._subscribers: dict[int, Callable[[], None]] = {}
        self._next_subscriber_id = 0

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        claimed = self._claim_callbacks()
        if claimed is None:
            return
        request_callback, abort, subscribers = claimed
        self._invoke_callback(request_callback)
        self._invoke_callback(abort)
        for subscriber in subscribers:
            self._invoke_callback(subscriber)

    def cancel_nonblocking(self) -> None:
        """Publish cancellation without serializing transport teardown.

        The durable request callback remains first. Provider abort and child
        subscriber callbacks are then fanned out independently so one blocking
        HTTP close cannot freeze the UI or delay cancellation of sibling
        children. Callback threads are daemonized because provider teardown is
        advisory; the cancelled turn still owns its normal retirement boundary.
        """

        claimed = self._claim_callbacks()
        if claimed is None:
            return
        request_callback, abort, subscribers = claimed
        self._invoke_callback(request_callback)
        for index, callback in enumerate((abort, *subscribers)):
            if not callable(callback):
                continue
            threading.Thread(
                target=self._invoke_callback,
                args=(callback,),
                name=f"alysis-cancel-callback-{index}",
                daemon=True,
            ).start()

    def _claim_callbacks(
        self,
    ) -> tuple[object | None, object | None, list[Callable[[], None]]] | None:
        with self._abort_lock:
            if self._event.is_set():
                return None
            self._event.set()
            request_callback = self._request_callback
            self._request_callback = None
            abort = self._abort
            self._abort = None
            subscribers = list(self._subscribers.values())
            self._subscribers.clear()
        return request_callback, abort, subscribers

    @staticmethod
    def _invoke_callback(callback: object | None) -> None:
        if callable(callback):
            try:
                callback()
            except Exception:
                pass

    def set_abort_callback(self, callback: object | None) -> None:
        with self._abort_lock:
            already_cancelled = self._event.is_set()
            if not already_cancelled:
                self._abort = callback
        if already_cancelled:
            self._invoke_callback(callback)

    def clear_abort_callback(self) -> None:
        with self._abort_lock:
            self._abort = None

    def set_request_callback(self, callback: object | None) -> None:
        """Invoke ``callback`` once at the cancellation publication boundary."""

        with self._abort_lock:
            already_cancelled = self._event.is_set()
            if not already_cancelled:
                self._request_callback = callback
        if already_cancelled:
            self._invoke_callback(callback)

    def clear_request_callback(self) -> None:
        with self._abort_lock:
            self._request_callback = None

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Invoke ``callback`` once when cancellation is first published.

        Registration is race-safe with :meth:`cancel`: a callback registered
        after cancellation runs immediately, while an unsubscribed callback is
        omitted unless cancellation already claimed it for delivery.
        """

        with self._abort_lock:
            already_cancelled = self._event.is_set()
            if already_cancelled:
                subscriber_id: int | None = None
            else:
                subscriber_id = self._next_subscriber_id
                self._next_subscriber_id += 1
                self._subscribers[subscriber_id] = callback
        if already_cancelled:
            self._invoke_callback(callback)

        def _unsubscribe() -> None:
            if subscriber_id is None:
                return
            with self._abort_lock:
                self._subscribers.pop(subscriber_id, None)

        return _unsubscribe

    def throw_if_cancelled(self, reason: str = "cancelled_by_user") -> None:
        if self.is_cancelled:
            raise KeyboardInterrupt(reason)

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until cancellation or ``timeout`` without polling."""

        return self._event.wait(timeout)

    def wait_cancelled(self, timeout: float | None = None) -> bool:
        """Wait for cancellation without exposing the backing event."""

        return self.wait(timeout)


def raise_if_cancelled(cancellation_token: Any | None) -> None:
    """Raise the token's native cancellation outcome when it is cancelled."""

    if cancellation_token is None or not bool(getattr(cancellation_token, "is_cancelled", False)):
        return
    throw_if_cancelled = getattr(cancellation_token, "throw_if_cancelled", None)
    if callable(throw_if_cancelled):
        throw_if_cancelled("cancelled_by_user")
    raise KeyboardInterrupt("cancelled_by_user")
