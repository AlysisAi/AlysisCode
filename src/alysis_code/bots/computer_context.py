"""Which task is opening a computer, for the code that builds and restores it.

A task that pauses for the person (an approval, a question, background hand-off) leaves its
own workspace and browser checkpoint behind while it is still active. When that same task
resumes, its computer must continue from that checkpoint; a different task must still wait
until the paused one ends. The computer is built before it is bound to a task, so the opener
names the task here for the duration of the build.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_OPENING: ContextVar[str | None] = ContextVar("alysis_bots_opening_task", default=None)


def opening_task() -> str | None:
    return _OPENING.get()


@contextmanager
def opening_for(task_id: str | None) -> Iterator[None]:
    token = _OPENING.set(task_id)
    try:
        yield
    finally:
        _OPENING.reset(token)
