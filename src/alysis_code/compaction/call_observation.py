"""Observe actual compactor calls without counting local preflight as model latency."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

_observer: ContextVar[Callable[[float], None] | None] = ContextVar(
    "compactor_llm_duration_observer", default=None
)
_logger = logging.getLogger(__name__)


@contextmanager
def observe_compactor_llm_durations(observer: Callable[[float], None]) -> Iterator[None]:
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


@contextmanager
def measure_compactor_llm_call() -> Iterator[None]:
    observer = _observer.get()
    if observer is None:
        yield
        return
    started = perf_counter()
    try:
        yield
    finally:
        try:
            observer(max(0.0, perf_counter() - started))
        except Exception:
            # Accounting must not replace a provider failure or cancellation.
            _logger.warning("Could not record compactor model-call duration", exc_info=True)
