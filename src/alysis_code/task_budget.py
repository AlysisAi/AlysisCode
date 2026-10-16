"""Process-only authority for a single supervised, explicitly budgeted IDE turn.

No payload, approval, permit or counter is persisted here. Main owns durable
accounting; an exact acknowledgement precedes each actual provider transport.
"""

from __future__ import annotations

import copy
import hashlib
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from .cancellation import CooperativeCancellationError
from .execution_deadline import ExecutionDeadline


class TaskBudgetRefused(CooperativeCancellationError):
    def __init__(self) -> None:
        super().__init__("task_budget_refused")


def need(value: Any) -> None:
    if not value:
        raise TaskBudgetRefused()


def stamp(value: str) -> float:
    need(isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", value))
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        raise TaskBudgetRefused() from None


def iso(value: float) -> str:
    return (
        datetime.fromtimestamp(value, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def parse_binding(raw: Any) -> dict[str, Any]:
    need(
        type(raw) is dict
        and set(raw)
        == {
            "version",
            "taskId",
            "envelopeId",
            "authorityGeneration",
            "deadlineAt",
            "leaseId",
            "leaseExpiresAt",
        }
    )
    need(type(raw["version"]) is int and raw["version"] == 1)
    need(type(raw["authorityGeneration"]) is int and 0 < raw["authorityGeneration"] <= 2**53 - 1)
    for key in ("taskId", "envelopeId", "leaseId"):
        need(
            isinstance(raw[key], str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", raw[key])
        )
    need(stamp(raw["leaseExpiresAt"]) <= stamp(raw["deadlineAt"]))
    return copy.deepcopy(raw)


_ACTIVE: ContextVar[TaskBudgetAuthority | None] = ContextVar("owned_task_budget", default=None)
_SETUP: ContextVar[bool] = ContextVar("owned_task_budget_setup", default=False)
_PURPOSE: ContextVar[str] = ContextVar("owned_task_budget_purpose", default="auxiliary")


def current_task_budget() -> TaskBudgetAuthority | None:
    return _ACTIVE.get()


def provider_auth_network_allowed() -> bool:
    return not _SETUP.get() and current_task_budget() is None


@contextmanager
def task_budget_setup(enabled: bool) -> Iterator[None]:
    # No attempt channel exists until a real job starts. Metadata may remain
    # offline; setup must not discover models or refresh credentials implicitly.
    token = _SETUP.set(_SETUP.get() or enabled)
    try:
        yield
    finally:
        _SETUP.reset(token)


@contextmanager
def task_budget_purpose(purpose: str) -> Iterator[None]:
    need(purpose in {"main", "compaction", "auxiliary"})
    token = _PURPOSE.set(purpose)
    try:
        yield
    finally:
        _PURPOSE.reset(token)


class TaskBudgetAuthority:
    def __init__(
        self,
        binding: dict[str, Any],
        *,
        exchange: Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]],
        assert_current: Callable[[], None],
        cancelled: Callable[[], bool],
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._binding = parse_binding(binding)
        self._exchange, self._assert_current, self._cancelled = exchange, assert_current, cancelled
        self._now, self._monotonic = now, monotonic
        self._last_wall = now()
        self._deadline = monotonic() + max(0, stamp(binding["deadlineAt"]) - self._last_wall)
        self._lease_deadline = monotonic() + max(
            0, stamp(binding["leaseExpiresAt"]) - self._last_wall
        )
        self._lock = threading.RLock()
        self._pending: str | None = None
        self._sequence = 0
        self._closed = False
        self._used = False
        self._host_renewal: str | None = None

    @property
    def binding(self) -> dict[str, Any]:
        return copy.deepcopy(self._binding)

    @property
    def is_cancelled(self) -> bool:
        return (
            self._closed
            or self._cancelled()
            or self._now() < self._last_wall
            or self._now() >= stamp(self._binding["deadlineAt"])
            or self._monotonic() >= self._deadline
        )

    def throw_if_cancelled(self, _reason: str = "") -> None:
        self.check()

    def check(self, *, lease: bool = False) -> None:
        try:
            now = self._now()
            need(now >= self._last_wall and not self.is_cancelled)
            self._last_wall = now
            self._assert_current()
            need(not self.is_cancelled)
            if lease:
                need(
                    self._now() < stamp(self._binding["leaseExpiresAt"])
                    and self._monotonic() < self._lease_deadline
                )
        except BaseException:
            self.close()
            raise

    def remaining(self) -> float:
        self.check()
        return max(
            0,
            min(
                self._deadline - self._monotonic(), stamp(self._binding["deadlineAt"]) - self._now()
            ),
        )

    def _request(self, kind: str, value: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self._exchange(kind, value, self.binding)
            need(type(result) is dict and result.get("ok") is True)
            if kind == "renew":
                need(set(result) == {"ok", "task_budget"})
                after = parse_binding(result["task_budget"])
                need(
                    all(
                        after[key] == self._binding[key]
                        for key in ("taskId", "envelopeId", "authorityGeneration", "deadlineAt")
                    )
                    and after["leaseId"] != self._binding["leaseId"]
                )
            else:
                need(set(result) == {"ok"})
            return result
        except BaseException:
            self.close()
            raise

    def execution_deadline(self, previous: ExecutionDeadline | None) -> ExecutionDeadline:
        deadline = self._deadline
        if previous is not None and previous.deadline_monotonic is not None:
            deadline = min(deadline, previous.deadline_monotonic)
        return ExecutionDeadline.from_absolute(
            started_at_monotonic=self._monotonic(),
            deadline_monotonic=deadline,
            clock=self._monotonic,
        )

    @contextmanager
    def activate(self) -> Iterator[None]:
        need(not self._used and current_task_budget() is None)
        self._used = True
        self.check(lease=True)
        token = _ACTIVE.set(self)
        try:
            yield
        finally:
            self.close()
            _ACTIVE.reset(token)

    def ensure_lease(self, *, force: bool = False) -> None:
        with self._lock:
            self.check()
            if (
                not force
                and self._now() < stamp(self._binding["leaseExpiresAt"])
                and self._monotonic() < self._lease_deadline
            ):
                return
            need(self._pending is None)
            before = self.binding
            result = self._request("renew", {})
            self.check()
            need(result.get("ok") is True and set(result) == {"ok", "task_budget"})
            after = parse_binding(result["task_budget"])
            need(
                all(
                    after[key] == before[key]
                    for key in ("taskId", "envelopeId", "authorityGeneration", "deadlineAt")
                )
                and after["leaseId"] != before["leaseId"]
            )
            self._binding = after
            self._lease_deadline = self._monotonic() + max(
                0, stamp(after["leaseExpiresAt"]) - self._now()
            )
            self.check(lease=True)

    def begin(
        self, *, method: str, url: str, headers: list[tuple[bytes, bytes]], body: bytes
    ) -> str:
        with self._lock:
            self.ensure_lease()
            need(
                self._pending is None and self._sequence < 10_000 and len(body) <= 32 * 1024 * 1024
            )
            # Length-delimited immutable prepared bytes; only the digest leaves this process.
            digest = hashlib.sha256()
            for value in [
                method.encode(),
                url.encode(),
                *[part for pair in headers for part in pair],
                body,
            ]:
                digest.update(len(value).to_bytes(8, "big"))
                digest.update(value)
            self._sequence += 1
            attempt = "attempt_" + uuid.uuid4().hex
            self._pending = attempt  # consume before exchange; loss never permits retry
            result = self._request(
                "attempt",
                {
                    "version": 1,
                    "attemptId": attempt,
                    "sequence": self._sequence,
                    "leaseId": self._binding["leaseId"],
                    "requestDigest": "sha256:" + digest.hexdigest(),
                    "purpose": _PURPOSE.get(),
                },
            )
            need(result == {"ok": True})
            self.check(lease=True)
            need(self._pending == attempt)
            return attempt

    def settled(self, attempt: str) -> None:
        with self._lock:
            need(self._pending == attempt)
            self.check()
            result = self._request("settled", {"attemptId": attempt})
            need(result == {"ok": True})
            self.check()
            self._pending = None

    def renew_after_host_refusal(self) -> dict[str, str]:
        self.ensure_lease(force=True)
        need(self._host_renewal is None)
        self._host_renewal = self._binding["leaseId"]
        return dict(_RENEWED_HOST_RESPONSE)

    def consume_host_renewal(self, result: Any) -> bool:
        # A result code cannot request recovery. Only the actual private renewal
        # ACK for this still-current lease arms this one-use turn-loop receipt.
        if self._host_renewal is None or result != _RENEWED_HOST_RESPONSE:
            return False
        lease = self._host_renewal
        self._host_renewal = None
        self.check(lease=True)
        need(lease == self._binding["leaseId"])
        return True

    def close(self) -> None:
        self._closed = True
        self._host_renewal = None


def refuse_budgeted_auth_refresh() -> None:
    if not provider_auth_network_allowed():
        raise TaskBudgetRefused()


_RENEWED_HOST_RESPONSE = {
    "error": "computer_use_lease_renewal_required",
    "message": "The short lease has now been renewed after an independent check. This call was not performed. Read the current page before choosing the next action.",
}
