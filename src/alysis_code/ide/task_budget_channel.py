"""Transient exact-job budget rendezvous on the already authenticated IDE pipe."""

from __future__ import annotations

import copy
import hmac
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from ..task_budget import iso, need, parse_binding, stamp


class TaskBudgetChannel:
    def __init__(
        self,
        binding: dict[str, Any],
        operation_id: str,
        *,
        emit: Callable[[dict[str, Any]], None],
        assert_current: Callable[[], str],
    ) -> None:
        self.binding = parse_binding(binding)
        self.operation_id, self._emit, self._assert_current = operation_id, emit, assert_current
        self._lock = threading.RLock()
        self._pending: dict[str, Any] | None = None
        self._count = 0
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._pending:
                self._pending["done"].set()

    def exchange(
        self, kind: str, payload: dict[str, Any], binding: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            with self._lock:
                need(
                    not self._closed
                    and self._pending is None
                    and self._count < 32_768
                    and binding == self.binding
                )
                self._count += 1
                job = self._assert_current()
                expiry = min(
                    time.time() + 29,
                    stamp(
                        binding["leaseExpiresAt"] if kind == "attempt" else binding["deadlineAt"]
                    ),
                )
                need(expiry > time.time())
                data = {
                    "version": 1,
                    "requestId": "budget_" + uuid.uuid4().hex,
                    "responseNonce": uuid.uuid4().hex + uuid.uuid4().hex,
                    "operationId": self.operation_id,
                    "leaseId": binding["leaseId"],
                    "expiresAt": iso(expiry),
                    "kind": kind,
                    "request": copy.deepcopy(payload),
                }
                pending = {
                    "data": data,
                    "job": job,
                    "done": threading.Event(),
                    "response": None,
                    "deadline": time.monotonic() + max(0, expiry - time.time()),
                }
                self._pending = pending
                need(self._assert_current() == job and not self._closed)
                self._emit(data)
            while not pending["done"].wait(0.025):
                need(
                    not self._closed
                    and time.monotonic() < pending["deadline"]
                    and time.time() < expiry
                )
                need(self._assert_current() == job)
            with self._lock:
                need(
                    not self._closed
                    and self._pending is pending
                    and time.monotonic() < pending["deadline"]
                    and time.time() < expiry
                )
                need(self._assert_current() == job and type(pending["response"]) is dict)
                self._pending = None
                return pending["response"]
        except BaseException:
            self.close()
            raise

    def respond(
        self, params: dict[str, Any], session_id: str
    ) -> tuple[dict[str, Any], Callable[[], None]]:
        with self._lock:
            pending = self._pending
            need(not self._closed and pending is not None and pending["response"] is None)
            need(
                time.monotonic() < pending["deadline"]
                and time.time() < stamp(pending["data"]["expiresAt"])
            )
            need(self._assert_current() == pending["job"])
            data = pending["data"]
            expected = {
                "session_id": session_id,
                "operation_id": self.operation_id,
                "job_id": pending["job"],
                "request_id": data["requestId"],
                "response_nonce": data["responseNonce"],
                "lease_id": data["leaseId"],
            }
            for key, value in expected.items():
                need(type(params.get(key)) is str and hmac.compare_digest(params[key], value))
            need(type(params.get("ok")) is bool)
            response: dict[str, Any] = {"ok": params["ok"]}
            if not params["ok"]:
                need(
                    set(params) == {*expected, "ok", "code"}
                    and params["code"]
                    in {
                        "unavailable",
                        "budget_exhausted",
                        "renewal_required",
                        "clock_regressed",
                        "authority_refused",
                    }
                )
                response["code"] = params["code"]
            elif data["kind"] == "renew":
                need(set(params) == {*expected, "ok", "task_budget"})
                after = parse_binding(params["task_budget"])
                need(
                    all(
                        after[key] == self.binding[key]
                        for key in ("taskId", "envelopeId", "authorityGeneration", "deadlineAt")
                    )
                    and after["leaseId"] != self.binding["leaseId"]
                    and stamp(after["leaseExpiresAt"]) > time.time()
                )
                response["task_budget"] = after
            else:
                need(set(params) == {*expected, "ok"})
            need(
                self._assert_current() == pending["job"]
                and self._pending is pending
                and not self._closed
            )
            if "task_budget" in response:
                self.binding = copy.deepcopy(response["task_budget"])
            pending["response"] = response

            # Wake only after the stdio result has actually been flushed. An
            # immediate HTTP response must not emit `settled` ahead of its ACK.
            def flushed() -> None:
                with self._lock:
                    if not self._closed and self._pending is pending:
                        pending["done"].set()

            return {
                "session_id": session_id,
                "request_id": data["requestId"],
                "status": "resolved",
            }, flushed
