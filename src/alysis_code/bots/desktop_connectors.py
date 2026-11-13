"""Plugins for owls, reached through the desktop app that keeps their sign-ins (2026-11-10).

The desktop app holds every plugin sign-in on the person's computer, as in Work. It tells this
service which plugin tools it can call (with Alysis's approval effect for each) and answers this
service's calls one at a time; the service never receives a token. An owl's call waits until the
app answers, the time runs out or the task stops. Without the app, plugins are unavailable and the
owl says so. Plugins are account-wide: every owl of this service can use every connected plugin.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from ..cloud_connections import ConnectionUnavailable
from .models import Input
from .store import NotFound

Effect = Literal["read", "own_change", "asks"]
CALL_TIMEOUT_S = 90.0
POLL_WAIT_S = 8.0
# The app polls continuously while it runs; a longer silence means it is closed or stopped.
DESKTOP_IDLE_S = 30.0
MAX_BATCH = 8


class CatalogTool(Input):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    effect: Effect


class CatalogPlugin(Input):
    plugin_id: str = Field(pattern=r"^(alysis|custom)\.[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    tools: list[CatalogTool] = Field(max_length=96)


class CatalogInput(Input):
    revision: int = Field(ge=0)
    plugins: list[CatalogPlugin] = Field(max_length=64)


class ResultInput(Input):
    ok: bool
    text: str = Field(default="", max_length=60000)
    is_error: bool = False
    dispatch_state: Literal["not_dispatched", "dispatched", "unknown"] = "dispatched"
    error: str = Field(default="", max_length=1000)


class PluginCallUnavailable(ConnectionUnavailable):
    """Safe to show; ``dispatched`` says whether the app may have sent the call."""

    def __init__(self, message: str, *, dispatched: bool):
        super().__init__(message)
        self.dispatched = dispatched


@dataclass
class _Call:
    id: str
    bot_id: str
    task_id: str
    plugin_id: str
    tool: str
    arguments: dict[str, Any]
    approval: str
    claimed: bool = False
    result: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "bot_id": self.bot_id,
            "task_id": self.task_id,
            "plugin_id": self.plugin_id,
            "name": self.tool,
            "arguments": self.arguments,
            "approval": self.approval,
        }


class DesktopConnectors:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Condition()
        self._plugins: list[CatalogPlugin] = []
        self._revision = -1
        self._seen: float | None = None
        self._calls: dict[str, _Call] = {}

    # ---- the desktop app --------------------------------------------------------------------
    def publish(self, data: CatalogInput) -> dict[str, Any]:
        with self._lock:
            self._plugins, self._revision = list(data.plugins), data.revision
            self._seen = self._clock()
            self._lock.notify_all()
        return {"revision": data.revision, "plugins": len(data.plugins)}

    def next(self, wait: float = POLL_WAIT_S) -> dict[str, Any]:
        deadline = self._clock() + wait
        with self._lock:
            while True:
                self._seen = self._clock()
                ready = [call for call in self._calls.values() if not call.claimed][:MAX_BATCH]
                if ready:
                    for call in ready:
                        call.claimed = True
                    return {"revision": self._revision, "calls": [c.public() for c in ready]}
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return {"revision": self._revision, "calls": []}
                self._lock.wait(remaining)

    def answer(self, call_id: str, data: ResultInput) -> dict[str, Any]:
        with self._lock:
            self._seen = self._clock()
            call = self._calls.get(call_id)
            if call is None or not call.claimed or call.result is not None:
                raise NotFound("This plugin call is no longer waiting.")
            call.result = data.model_dump()
            self._lock.notify_all()
        return {"accepted": True}

    # ---- owls ---------------------------------------------------------------------------------
    def _available(self) -> bool:
        return self._seen is not None and self._clock() - self._seen <= DESKTOP_IDLE_S

    def available(self) -> bool:
        with self._lock:
            return self._available()

    def plugins(self) -> list[CatalogPlugin]:
        """The connected plugins, only while the app answers."""
        with self._lock:
            return list(self._plugins) if self._available() else []

    def call(
        self,
        *,
        bot_id: str,
        task_id: str,
        plugin_id: str,
        tool: str,
        arguments: dict[str, Any],
        approval: str,
        token: Any = None,
    ) -> dict[str, Any]:
        call = _Call(uuid4().hex, bot_id, task_id, plugin_id, tool, arguments, approval)
        with self._lock:
            if not self._available():
                raise PluginCallUnavailable(
                    "Plugins are reached through Alysis on this computer, and it isn't "
                    "connected right now. Nothing was sent. Open Alysis, then try again.",
                    dispatched=False,
                )
            self._calls[call.id] = call
            self._lock.notify_all()
            deadline = self._clock() + CALL_TIMEOUT_S
            try:
                while call.result is None:
                    if token is not None:
                        token.throw_if_cancelled()
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise PluginCallUnavailable(
                            "Alysis on this computer didn't answer in time. "
                            + (
                                "The call may have run; check before trying again."
                                if call.claimed
                                else "Nothing was sent."
                            ),
                            dispatched=call.claimed,
                        )
                    self._lock.wait(min(remaining, 1.0))
            finally:
                self._calls.pop(call.id, None)
        return call.result
