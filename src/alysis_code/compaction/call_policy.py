"""Scope turn-owned admission to actual compaction provider operations."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from typing import Any

ProviderCallPolicy = Callable[[Any], AbstractContextManager[None]]
_policy: ContextVar[ProviderCallPolicy | None] = ContextVar(
    "compaction_provider_call_policy", default=None
)


@contextmanager
def compaction_provider_policy(policy: ProviderCallPolicy) -> Iterator[None]:
    token = _policy.set(policy)
    try:
        yield
    finally:
        _policy.reset(token)


@contextmanager
def compaction_provider_call(client: Any) -> Iterator[None]:
    """Leave local planning free of request admission and timeout mutation."""
    policy = _policy.get()
    if policy is None:
        yield
    else:
        with policy(client):
            yield
