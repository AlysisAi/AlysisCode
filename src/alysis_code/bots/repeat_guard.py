"""Stop a model that keeps making the same tool call.

A stuck model can send one call again and again: a call that fails the same way every time,
or one whose result it already has, until the task's context or the account's allowance runs
out. A call is known by its tool and its arguments (as canonical JSON), its outcome by a digest
of its result; only digests are kept, never argument or result text.

- **The same failure.** After one call has failed the same way ``failures`` times it is not run
  again: the model gets ``{"error": …, "executed": false}`` saying why and what to do instead.
  A step that worked in between (something may have changed) lets it run once more.
- **The same result.** A call that returned exactly the same successful result ``note`` times
  gets a note beside that result: use what you already have, or wait before checking again.
- **No way out.** ``stop`` attempts of one call with the same failure (refusals included) end
  the turn with a plain failure, and so do ``stop`` back-to-back calls with the same successful
  result. Only back-to-back repeats count for a successful result, so work that waits in steps
  (wait, check, wait, check) carries on with the note alone.
- **A fresh look.** A result that carries an image is new information: it never counts as a
  repeat, and it lets a refused call run again.

Turns nobody is watching (routine runs, work off the reply loop, peer exchanges, activations)
use lower thresholds: nobody is there to press Stop. Everything is decided from the calls'
structure and results, never from a tool's words, the language or the model.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from . import failures
from .store import LimitReached


@dataclass(frozen=True)
class Thresholds:
    # Identical failures of one call before it is refused.
    failures: int
    # Identical successful results of one call before it gets a note.
    note: int
    # Attempts with the same outcome before the turn stops.
    stop: int


WATCHED = Thresholds(failures=3, note=3, stop=10)
UNWATCHED = Thresholds(failures=2, note=2, stop=6)

REPEAT_STOPPED = (
    "The owl kept repeating the same step with the same result, so it stopped instead of using "
    "up your allowance. Anything it saved is still in its Library. Try asking again with more "
    "detail, or with a different model."
)


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def failed(result: Any) -> bool:
    """Whether a tool result is an error or refusal rather than a result."""
    return isinstance(result, dict) and "error" in result


@dataclass
class _Call:
    outcome: str | None = None
    failed: bool = False
    # Attempts with this outcome, refusals included.
    same: int = 0
    # Times the call actually ran and had this outcome.
    ran: int = 0
    # Back-to-back calls with this outcome.
    streak: int = 0
    # The guard's step count at this call's last attempt.
    step: int = 0


class RepeatGuard:
    """One turn's record of its tool calls."""

    def __init__(self, *, watched: bool) -> None:
        self.limits = WATCHED if watched else UNWATCHED
        self._calls: dict[str, _Call] = {}
        self._step = 0
        # The step of the last call that worked (a result, not an error).
        self._worked = 0
        # The key of the previous call, for back-to-back repeats.
        self._previous: str | None = None

    @staticmethod
    def _key(name: str, arguments: Any) -> str:
        return _digest([name, arguments])

    def before(self, name: str, arguments: Any) -> dict | None:
        """None to run the call, or the refusal to return in its place.

        Raises a MODEL failure when the call has had the same outcome too many times.
        """
        key = self._key(name, arguments)
        record = self._calls.get(key)
        if record is None or record.outcome is None:
            return None
        back_to_back = self._previous == key
        if (record.failed and record.same >= self.limits.stop) or (
            not record.failed and back_to_back and record.streak >= self.limits.stop
        ):
            raise failures.with_kind(LimitReached(REPEAT_STOPPED), failures.MODEL)
        if not record.failed or record.ran < self.limits.failures:
            return None
        if self._worked > record.step:
            return None  # Another step worked since: something may have changed.
        self._step += 1
        self._previous = key
        record.same += 1
        record.streak = record.streak + 1 if back_to_back else 1
        record.step = self._step
        return {
            "error": (
                f"This exact call has failed the same way {record.ran} times, so it was not run "
                "again. Its earlier result says why. Do something different: fix what that error "
                "names, change the arguments, use another tool, or tell the person what is "
                "blocking you. After another step works, this call can be tried again."
            ),
            "executed": False,
        }

    def after(self, name: str, arguments: Any, result: Any, *, fresh_look: bool = False):
        """Record a call's outcome; returns a note to give the model with it, or None."""
        key = self._key(name, arguments)
        self._step += 1
        back_to_back = self._previous == key
        self._previous = key
        record = self._calls.setdefault(key, _Call())
        record.step = self._step
        if fresh_look:
            # An image is a new look at the world: never a repeat, and new information.
            record.outcome, record.failed = None, False
            record.same = record.ran = record.streak = 0
            self._worked = self._step
            return None
        outcome = _digest(result)
        is_failure = failed(result)
        if not is_failure:
            self._worked = self._step
        if outcome == record.outcome:
            record.same += 1
            record.ran += 1
            record.streak = record.streak + 1 if back_to_back else 1
        else:
            record.outcome, record.failed = outcome, is_failure
            record.same = record.ran = record.streak = 1
        if is_failure or record.ran < self.limits.note:
            return None
        return (
            f"This call returned exactly the same result {record.ran} times. Use the result you "
            "already have instead of asking again. If you are waiting for something to change, "
            "wait before checking again, or tell the person what you are waiting for."
        )

    def raised(self, name: str, arguments: Any, exc: BaseException) -> None:
        """Record a call that raised: its outcome is the error's type and text."""
        self.after(name, arguments, {"error": [type(exc).__name__, str(exc)]})


def with_note(result: Any, note: str | None) -> Any:
    """A tool result with the guard's note beside it."""
    if note is None:
        return result
    if isinstance(result, dict):
        return {**result, "repeat_notice": note}
    return {"result": result, "repeat_notice": note}
