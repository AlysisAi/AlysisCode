"""Serialize local planning, integration and navigation actions with chat turns."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class IdleBoundaryError(RuntimeError):
    pass


@dataclass(frozen=True)
class TurnOutcome:
    result: Any = None
    exception: BaseException | None = None
    cancelled: bool = False


class TuiLocalActionLifecycle:
    """Retain a live-session reservation until the actual worker completes."""

    def __init__(self):
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-actions")
        self._turn = False
        self._mutation = False
        self._closed = False
        self.session_provider: Callable[[], Any] = lambda: None
        self.idle: Callable[[], bool] = lambda: False
        self.ordinary_gate: Callable[[], None] = lambda: None

    @property
    def mutating(self):
        with self._lock:
            return self._mutation

    @staticmethod
    def identity_for(session):
        store = getattr(session, "store", None)
        session_id = str(getattr(store, "session_id", "") or "")
        workspace = getattr(store, "workspace_root", None) or getattr(session, "root", None)
        if not session_id or workspace is None:
            return None
        return session_id, str(Path(workspace).resolve())

    def begin_turn(self, session, *, check_gate=True):
        with self._lock:
            if self._closed or self._turn or self._mutation:
                raise IdleBoundaryError("Wait for active and local work to finish.")
            self._turn = True
        try:
            if check_gate:
                self.ordinary_gate()
        except BaseException:
            self.finish_turn()
            raise

    def finish_turn(self, *, failed=False, outcome=None):
        with self._lock:
            self._turn = False

    def submit_local(self, expected, operation) -> Future:
        with self._lock:
            if self._closed or self._turn or self._mutation or not self.idle():
                raise IdleBoundaryError("Wait for active and queued work to finish.")
            if expected is None or self.identity_for(self.session_provider()) != expected:
                raise IdleBoundaryError("Session or workspace changed; reopen the view.")
            self._mutation = True

            def execute():
                try:
                    if self.identity_for(self.session_provider()) != expected:
                        raise IdleBoundaryError("Session changed before execution.")
                    self.ordinary_gate()
                    return operation()
                finally:
                    with self._lock:
                        self._mutation = False

            try:
                context = copy_context()
                future = self._executor.submit(context.run, execute)
            except BaseException:
                self._mutation = False
                raise

            def cancelled(done):
                if done.cancelled():
                    with self._lock:
                        self._mutation = False

            future.add_done_callback(cancelled)
            return future

    def shutdown(self):
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)
