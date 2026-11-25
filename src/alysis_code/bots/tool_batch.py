"""Several reads that one model response asks for run side by side.

A model may ask for several steps in one response. They run in the order asked, except that
consecutive reads of the web or of the owl's own records run at the same time: four pages take
about as long as the slowest of them instead of all four one after another. Any other step is a
barrier: it waits for the reads before it, and the reads after it start once it has finished.

Only the reading itself runs on a worker thread. Everything the turn keeps about its steps stays
in the turn's own thread, in the order asked: the checks before a step (Stop, computer control,
the repeat guard), its started and finished receipts, and its result for the model. A group's
checks all pass before any of its reads starts, and each read's started receipt is written as
it starts, so a receipt never names a read that was only waiting. Stop ends the turn while reads
are still running; their results are dropped and nothing after them starts.

The checks decide as they would if the calls ran one by one. A read joins the reads asked just
before it only when its checks come out the same whatever those return (``joins``; for the
repeat guard, RepeatGuard.joins). Any other read waits for them and is checked after them.

Which reads may run side by side is declared below, never decided from a tool's words, the
language or the model. ``step_effects.READING`` lists every step that changes nothing outside
the owl's records, but not all of those can share a turn: browser steps share the owl's one
browser (opening, switching or closing a page changes what the next one sees), computer steps
its one desktop, research helpers think with the turn's own model and allowances, document
steps share the turn's scratch folder and the office engine, a table query builds every table
in memory, and reading a browser draft settles expired questions as it reads. Those run alone,
as does any step added later until it is listed here.
"""

from __future__ import annotations

import contextvars
import json
import threading
from collections.abc import Callable, Collection, Iterable, Iterator
from concurrent.futures import Future, wait
from typing import Any

from ..cancellation import CooperativeCancellationError
from .step_effects import READING

# Reads one group runs at the same time, each on its own thread; a longer run of reads continues
# in the next group. As many connections as web browsers open to one site at once.
WORKERS = 6
# How often a turn waiting for its reads looks for Stop.
POLL_SECONDS = 0.1

# Reads that touch nothing the turn holds: one search or page of the public web per call (the
# web lookup keeps nothing between calls), or the owl's own records, each read in its own store
# transaction, with no write. The owl's tool runner (BotRuntime._tool) answers each of them.
SIDE_BY_SIDE = READING & frozenset(
    {
        "web_search",
        "web_fetch",
        "list_local_computers",
        "read_tool_result",
        "list_schedules",
        "list_triggers",
        "list_peer_bots",
        "search_history",
        "read_message",
        "list_memories",
        "read_memory",
        "memory_history",
        "memory_source_read",
        "read_working_note",
        "search_working_notes",
        "list_files",
        "read_file",
        "read_playbook",
    }
)


def side_by_side(
    offered: Collection[str], *, elsewhere: Iterable[Collection[str]]
) -> Callable[[Any], bool]:
    """Whether a call may run beside its neighbours: a read listed in SIDE_BY_SIDE that the
    turn offers and sends to the owl's own tool runner, not to a computer or a plugin of the
    same name (``elsewhere``: the names each of those takes)."""
    offered = frozenset(offered)
    taken = frozenset(name for names in elsewhere for name in names)
    return lambda call: (
        call.name in SIDE_BY_SIDE and call.name in offered and call.name not in taken
    )


def _key(call) -> str:
    # The same tool with the same arguments, as the repeat guard tells calls apart.
    return json.dumps(
        [call.name, call.arguments],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def groups(calls: Iterable, together: Callable[[Any], bool], *, size: int = WORKERS) -> list[list]:
    """The calls in the order asked, in groups: consecutive calls that ``together`` admits share
    a group of at most ``size``; every other call is a group of its own. The same call asked
    twice starts a new group, so the repeat guard has seen how the first one went."""
    result: list[list] = []
    for call in calls:
        last = result[-1] if result else None
        if (
            last is not None
            and len(last) < size
            and together(call)
            and together(last[0])
            and _key(call) not in {_key(other) for other in last}
        ):
            last.append(call)
        else:
            result.append([call])
    return result


class Batch:
    """The calls of one model response, given to the turn in the order asked.

    Iterating yields each call with the refusal ``admit`` returned for it (None: run it). The
    turn takes each call's result with ``result``, which runs it now unless it already runs
    side by side with its group, and then waits for it, watching for Stop.

    ``admit``: the checks before a call (raises to end the turn; a refusal means not run).
    ``joins``: whether a call's checks come out the same whatever the calls asked just before it
    in its group return, so it may run beside them; asked as its group is about to start, after
    every earlier call's result is in. ``joining``: the checks for such a call, without those
    that depend on the calls before it (by default ``admit``). ``started``: writes the call's
    started receipt. ``run``: the owl's tool runner, called with the tool's name and arguments.
    """

    def __init__(
        self,
        calls: Iterable,
        *,
        together: Callable[[Any], bool],
        admit: Callable[[Any], dict | None],
        started: Callable[[Any], None],
        run: Callable[[str, Any], Any],
        token,
        joins: Callable[[Any], bool] = lambda call: True,
        joining: Callable[[Any], dict | None] | None = None,
        size: int = WORKERS,
    ) -> None:
        self._groups = groups(calls, together, size=size)
        self._admit, self._started, self._run = admit, started, run
        self._joins, self._joining = joins, joining or admit
        self._token = token
        self._running: dict[int, Future] = {}

    def _parts(self) -> Iterator[list]:
        """Each group, cut before the first call that cannot join the calls before it; that call
        starts the next part, checked once their results are in."""
        for group in self._groups:
            while group:
                part = [group[0]]
                for call in group[1:]:
                    if not self._joins(call):
                        break
                    part.append(call)
                yield part
                group = group[len(part) :]

    def __iter__(self) -> Iterator[tuple[Any, dict | None]]:
        try:
            for part in self._parts():
                head, *rest = part
                admitted = [(head, self._admit(head))]
                admitted += [(call, self._joining(call)) for call in rest]
                runnable = [call for call, refusal in admitted if refusal is None]
                if len(runnable) > 1:
                    for call in runnable:
                        self._started(call)
                        self._running[id(call)] = self._start(call)
                for call, refusal in admitted:
                    if refusal is None and id(call) not in self._running:
                        self._started(call)
                    yield call, refusal
                # Every read of a part was taken before the next part starts.
                self._running.clear()
        finally:
            self._running.clear()

    def result(self, call) -> Any:
        """The call's result; an error it raised is raised here, as if it ran here."""
        running = self._running.pop(id(call), None)
        if running is None:
            return self._run(call.name, call.arguments)
        while not wait((running,), timeout=POLL_SECONDS).done:
            if self._token.is_cancelled:
                # Reads have no effects: those still running finish on their own, unread.
                self._running.clear()
                self._token.throw_if_cancelled()
                raise CooperativeCancellationError()
        return running.result()

    def _start(self, call) -> Future:
        future: Future = Future()
        # The turn's allowances and the rest of its context go with the read.
        context = contextvars.copy_context()

        def read() -> None:
            future.set_running_or_notify_cancel()
            try:
                value = context.run(self._run, call.name, call.arguments)
            except BaseException as error:  # noqa: BLE001 - raised again in the turn's thread
                future.set_exception(error)
            else:
                future.set_result(value)

        # Like the runtime's other workers, a read never holds up the service as it stops.
        threading.Thread(target=read, name="owl-read", daemon=True).start()
        return future
