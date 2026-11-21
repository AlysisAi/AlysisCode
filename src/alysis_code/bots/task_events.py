"""A task's progress as it happens: ``POST /v1/tasks/{id}/events`` (capability
``live_task_events``).

A client following a working task asks for the events after the last one it has (``after``, an
event's ``seq``: events are numbered in the order their transactions committed) and how long it
will wait (``wait``). The answer comes as soon as there are any, or once the wait is over with
none; a task that has ended answers at once. It is a long poll, not a stream: a plain
request/response passes every proxy, load balancer and client transport the owls API already
goes through, and each answer is small and complete.

The feed carries progress only. The owl's reply is never in it: a draft stays unpublished until
the reply checker passes it and the turn completes, and a withheld draft is never published.
Events that quote or judge the draft (the checker's verdicts) or travel with the reply
(suggested next messages) come with their data withheld; a client reads the published reply,
and what goes with it, from the conversation and the task once the task has ended.

Waiting costs no polling: the store wakes a waiting request after each commit that wrote events
for its task (``TaskSignals``). The wake is in-process; an event another process wrote to a
shared database is read at the latest after ``RECHECK_SECONDS``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress

from pydantic import Field

from .models import Input

# The longest a request may wait. It stays below the timeouts of the clients and proxies the
# owls API is served through, and bounds how long a waiting request can hold up a shutdown.
MAX_WAIT_SECONDS = 10.0
# The most events one answer carries; ``more`` says when others already wait after them.
MAX_EVENTS = 200
# How often a waiting request reads the task again without a wake.
RECHECK_SECONDS = 5.0
# How many requests of one owner may wait at once; more are answered at once, without waiting.
MAX_WAITERS_PER_OWNER = 32

# Events whose data quotes or judges the owl's draft reply, or travels with the reply itself.
REPLY_EVENTS = frozenset(
    {"reply_review_finished", "reply_review_status_accepted", "reply_suggestions"}
)


class TaskEventsQuery(Input):
    """The events after ``after`` (0: from the first), waiting up to ``wait`` seconds for one."""

    after: int = Field(default=0, ge=0)
    wait: float = Field(default=0, ge=0, le=MAX_WAIT_SECONDS)
    limit: int = Field(default=100, ge=1, le=MAX_EVENTS)


class TaskSignals:
    """Wakes the requests waiting for a task's next events once the transaction that wrote them
    has committed. Safe across threads: the store notifies from whichever thread wrote."""

    def __init__(self, *, max_waiters: int = MAX_WAITERS_PER_OWNER) -> None:
        self._lock = threading.Lock()
        self._waiters: dict[str, set[Callable[[], None]]] = {}
        self._owners: dict[str, int] = {}
        self._max = max_waiters

    @contextmanager
    def watching(self, owner: str, task_id: str, wake: Callable[[], None]) -> Iterator[bool]:
        """Call ``wake`` after each commit that wrote events for the task, while inside.

        Yields False, registering nothing, when the owner already has the most waiting requests.
        """
        with self._lock:
            admitted = self._owners.get(owner, 0) < self._max
            if admitted:
                self._waiters.setdefault(task_id, set()).add(wake)
                self._owners[owner] = self._owners.get(owner, 0) + 1
        try:
            yield admitted
        finally:
            if admitted:
                with self._lock:
                    waiters = self._waiters.get(task_id)
                    if waiters is not None:
                        waiters.discard(wake)
                        if not waiters:
                            del self._waiters[task_id]
                    self._owners[owner] -= 1
                    if not self._owners[owner]:
                        del self._owners[owner]

    def notify(self, task_ids: Iterable[str]) -> None:
        if not task_ids:
            return
        with self._lock:
            wakes = [wake for task_id in task_ids for wake in self._waiters.get(task_id, ())]
        for wake in wakes:
            wake()


def public_event(row) -> dict:
    """An event as the feed gives it: as ``GET /v1/tasks/{id}`` lists it, without reply text."""
    event = {
        "seq": row["seq"],
        "task_id": row["task_id"],
        "kind": row["kind"],
        "data": json.loads(row["data"]),
        "created_at": row["created_at"],
    }
    if event["kind"] in REPLY_EVENTS:
        event["data"] = {}
        event["withheld"] = True
    return event


def read(store, owner: str, task_id: str, after: int, limit: int) -> dict:
    """The task's state now and its events after ``after``, oldest first."""
    with store.connect() as db:
        # The owner's own task only, like every task route: another owner's reads as missing.
        task = store._task(db, owner, task_id)
        rows = db.execute(
            "SELECT seq,task_id,kind,data,created_at FROM events WHERE task_id=? AND seq>? "
            "ORDER BY seq LIMIT ?",
            (task_id, after, limit + 1),
        ).fetchall()
    events = [public_event(row) for row in rows[:limit]]
    return {
        "task_id": task_id,
        "status": task["status"],
        "wait_reason": task.get("wait_reason"),
        "updated_at": task["updated_at"],
        "events": events,
        # What to send as ``after`` next time.
        "after": events[-1]["seq"] if events else after,
        "more": len(rows) > limit,
    }


async def follow(store, owner: str, task_id: str, query: TaskEventsQuery) -> dict:
    """The task's events after ``query.after``, as soon as there are any, within ``query.wait``."""
    from .store import TERMINAL

    loop = asyncio.get_running_loop()
    changed = asyncio.Event()

    def wake() -> None:
        # Called from the thread that committed the events.
        with suppress(RuntimeError):  # the request already ended and its loop closed
            loop.call_soon_threadsafe(changed.set)

    deadline = loop.time() + query.wait
    with store.task_signals.watching(owner, task_id, wake) as admitted:
        while True:
            # Cleared before reading, so events committed during the read wake the next wait.
            changed.clear()
            answer = await asyncio.to_thread(read, store, owner, task_id, query.after, query.limit)
            remaining = deadline - loop.time()
            if answer["events"] or answer["status"] in TERMINAL or not admitted or remaining <= 0:
                return answer
            with suppress(TimeoutError):
                await asyncio.wait_for(changed.wait(), min(remaining, RECHECK_SECONDS))
