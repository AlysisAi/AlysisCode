"""How much of a model request one owl turn may fill, and the room it makes when refused.

Two measures decide whether room must be made before a request is sent. Its token estimate is
compared with the turn's context allowance. The client's own measure of the request as it goes
on the wire (``request_capacity_ratio``) is compared with CAPACITY_MARGIN: a route whose gateway
admits a request by its size in bytes and messages says how much of that bound the request
uses (1.0 is the whole bound), and every other route says 0.0, which never decides anything.
The two disagree most for text in a script that takes several bytes a character and for tool
results full of JSON, so neither decides alone. Work's compactor reads the same measure
(conversation_compactor). The margin says when to make room, not what to refuse: a request
that cannot be made smaller and is still within the turn's allowance is sent, and the client's
own check, or the model, decides.

When the provider, or the client's own check before sending, still refuses a request as too
long and nothing ran, the turn makes room and sends it again, at most REFUSAL_RETRIES times: the
next request must fit ROOM_AFTER_REFUSAL times the size of the refused one, on both measures.
The smaller size holds for the rest of the turn, since the refusal showed that the real limit
is below the refused request. It never takes the newest step away: when nothing more can go,
the request is sent as the turn's own allowance fits it, unless it is no smaller than the one
refused, and then that refusal stands and fails the turn as it always did.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace

from ..failure_category import is_context_window_exceeded_error
from ..llm.types import LLMError
from ..request_estimation import estimate_request_tokens

# The share of the client's wire bound a request may use, as Work's compactor starts making
# room at (compaction settings, trigger_ratio).
CAPACITY_MARGIN = 0.9
# After a refusal, the next request must fit within this share of the refused one: the size
# Work's compactor makes room down to (compaction settings, target_ratio).
ROOM_AFTER_REFUSAL = 0.7
# Refusals one turn makes room for and sends again; the next one fails the turn as before.
REFUSAL_RETRIES = 2


def too_long(exc: BaseException) -> bool:
    """Whether a model call was refused because its request is too long for the model.

    The provider's error code, or the shared recognition of the provider and gateway context
    errors that Work's own recovery uses (failure_category.is_context_window_exceeded_error).
    """
    if not isinstance(exc, LLMError):
        return False
    current, seen = exc, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "provider_error_code", None) == "context_length_exceeded":
            return True
        current = current.__cause__ or current.__context__
    return is_context_window_exceeded_error(exc)


class RequestRoom:
    """One turn's measure of its requests to ``client``, and what it learned from refusals.

    ``record`` writes a step for the turn's trace (a ``context_window`` event's data).
    """

    def __init__(self, client, record: Callable[[dict], None] | None = None):
        self.client = client
        self.record = record
        self.refusals = 0
        # Learned from refusals: the most tokens and the largest wire share a request may have.
        self._tokens: int | None = None
        self._ratio: float | None = None
        # The request last fitted, as (tokens, wire share), and a refusal still to answer.
        self._sent: tuple[int, float] | None = None
        self._refused: tuple[int, float] | None = None
        self._refusal: BaseException | None = None

    def measure(self, messages: list[dict], tools=None) -> float:
        """The share of the client's wire bound this request uses; 0.0 without such a bound."""
        measure = getattr(self.client, "request_capacity_ratio", None)
        if not callable(measure):
            return 0.0
        ratio = measure(messages=messages, tools=tools or None)
        if (
            isinstance(ratio, bool)
            or not isinstance(ratio, int | float)
            or not math.isfinite(ratio)
            or ratio < 0
        ):
            raise ValueError("Invalid provider request capacity ratio")
        return float(ratio)

    def _limits(self, allowance: int) -> tuple[int, float]:
        """The token allowance and wire share a request is fitted to, refusals counted."""
        tokens, margin = allowance, CAPACITY_MARGIN
        if self._tokens is not None:
            tokens = min(tokens, self._tokens)
        if self._ratio is not None:
            margin = min(margin, self._ratio)
        return tokens, margin

    def over(
        self, messages: list[dict], tools, tokens: int, allowance: int, *, target: bool = True
    ) -> bool:
        """Whether a request of ``tokens`` estimated tokens needs room made before it is sent.

        ``target=False`` asks instead whether the route would refuse it whatever room is made:
        over the turn's own allowance, or over the client's whole wire bound.
        """
        if not target:
            return tokens > allowance or self.measure(messages, tools) >= 1.0
        limit, margin = self._limits(allowance)
        return tokens > limit or self.measure(messages, tools) >= margin

    def allowance(self, allowance: int, messages: list[dict], tools, tokens: int) -> int:
        """The token allowance for requests like this one, its size on the wire counted too.

        A request's wire share grows with its tokens about as its content does, so this one's
        share per token tells how many tokens of the same kind of content fit the margin.
        """
        limit, margin = self._limits(allowance)
        ratio = self.measure(messages, tools)
        if ratio > 0:
            limit = min(limit, int(tokens * margin / ratio))
        return max(1, limit)

    def sized(self, allowance: int) -> Callable[[list[dict], object], int]:
        """A token estimate that also counts a request's wire share against ``allowance``.

        It is above ``allowance`` exactly when either measure says the request does not fit.
        """
        _, margin = self._limits(allowance)

        def estimate(messages, tools):
            tokens = estimate_request_tokens(messages, tools)
            ratio = self.measure(messages, tools)
            if ratio >= margin:
                return max(tokens, allowance + 1)
            return max(tokens, math.ceil(ratio / margin * allowance)) if ratio > 0 else tokens

        return estimate

    def summary_sizing(self, limits, messages: list[dict], tools, tokens: int):
        """The allowances and estimate a summary of this turn's steps is sized with."""
        allowance = self.allowance(limits.context_tokens, messages, tools, tokens)
        return replace(limits, context_tokens=allowance), self.sized(allowance)

    def settle(self, messages: list[dict], tools, tokens: int) -> BaseException | None:
        """Note the request about to be sent; the refusal that stands when it is no smaller."""
        ratio = self.measure(messages, tools)
        if self._refused is not None:
            refusal = self._refusal
            refused_tokens, refused_ratio = self._refused
            self._refused = self._refusal = None
            if tokens >= refused_tokens and ratio >= refused_ratio:
                # Nothing more could go, so the same request would only be refused again.
                return refusal
        self._sent = (tokens, ratio)
        return None

    def refused(self, exc: BaseException) -> bool:
        """After a call that failed with nothing run: whether to make room and send it again."""
        if self._sent is None or self.refusals >= REFUSAL_RETRIES or not too_long(exc):
            return False
        tokens, ratio = self._sent
        self.refusals += 1
        target = max(1, int(tokens * ROOM_AFTER_REFUSAL))
        self._tokens = target if self._tokens is None else min(self._tokens, target)
        if ratio > 0:
            share = ratio * ROOM_AFTER_REFUSAL
            self._ratio = share if self._ratio is None else min(self._ratio, share)
        self._refused, self._refusal = self._sent, exc
        if self.record is not None:
            self.record(
                {
                    "reason": "request_too_long",
                    "retry": self.refusals,
                    "message": "The model could not take a request this long. Making room to "
                    "send it again; saved results remain available.",
                }
            )
        return True
