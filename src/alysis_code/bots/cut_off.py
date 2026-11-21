"""A model reply that the output limit cut off: an owl runs none of it and shows none of it.

The Responses client raises LLMOutputLimitError for such a reply; the Anthropic Messages and
chat-completions clients return it as an ordinary response (llm/output_limit.py). Every owl loop
that offers tools or publishes the reply it gets (the chat turn, computer work, research
helpers, a peer's reply) settles each call here, so a cut-off reply takes the same path on every
protocol:

- the call is recorded as ``incomplete`` (it was charged);
- none of its tool calls runs, even one whose arguments happen to parse, and none of its text is
  published or shown as progress; it is not replayed either, so the next reply is written whole;
- an ``output_cut_off`` event names the tools it was writing, never their arguments;
- a system note tells the model what happened and how to continue in smaller parts.

A turn recovers at most ``MAX_RECOVERIES`` times; the next cut-off ends it with the loop's own
plain failure (``output_limit_failure`` for a turn).

The call after the first cut-off may name a larger output allowance, once per turn, when the
turn's model has a catalogued output ceiling above the owl's allowance
(``RuntimeLimits.output_ceiling``): up to that ceiling and never more than the request leaves room
for in the model's window. A guessed ceiling never raises it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Any

from ..llm.output_limit import response_cut_off_by_output_limit
from ..request_estimation import estimate_request_tokens
from . import failures
from .store import LimitReached

# Cut-off replies one turn recovers from; the next one ends it.
MAX_RECOVERIES = 2

OUTPUT_LIMIT_FAILURE = (
    "The model reached its response limit. Try a smaller task; no incomplete answer was marked "
    "finished."
)

# The document tool that adds to a document already made (bots/owl_tools.py).
EDIT_DOCUMENT_TOOL = "edit_document"

# Room kept for images, as the owl's context fitting keeps it (BotRuntime._fit_context).
_IMAGE_RESERVE_TOKENS = 4096
# Tool names an event or a note names at most; a name looks like one a provider accepts.
_NAMED_TOOLS = 12
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def output_limit_failure() -> BaseException:
    """The turn's plain failure once its cut-off replies are used up."""
    return failures.with_kind(LimitReached(OUTPUT_LIMIT_FAILURE), failures.MODEL)


def written_tool_names(response: Any) -> list[str]:
    """The tools a cut-off reply was writing calls to: from its parsed calls and the provider's
    own message (a half-written call is often dropped while parsing). Names only, never
    arguments."""
    names: list[Any] = [getattr(call, "name", None) for call in response.tool_calls or []]
    raw = getattr(response, "raw", None)
    if isinstance(raw, Mapping):
        choices = raw.get("choices")  # chat completions
        message = (
            choices[0].get("message")
            if isinstance(choices, list) and choices and isinstance(choices[0], Mapping)
            else None
        )
        calls = message.get("tool_calls") if isinstance(message, Mapping) else None
        for call in calls if isinstance(calls, list) else []:
            function = call.get("function") if isinstance(call, Mapping) else None
            if isinstance(function, Mapping):
                names.append(function.get("name"))
        # Anthropic Messages content blocks and Responses output items.
        for key, kind in (("content", "tool_use"), ("output", "function_call")):
            items = raw.get(key)
            for item in items if isinstance(items, list) else []:
                if isinstance(item, Mapping) and item.get("type") == kind:
                    names.append(item.get("name"))
    named: list[str] = []
    for name in names:
        if isinstance(name, str) and _TOOL_NAME.fullmatch(name) and name not in named:
            named.append(name)
    return named[:_NAMED_TOOLS]


def cut_off_note(written: list[str], offered: list[str]) -> str:
    """What the model is told after a cut-off reply: what happened and how to go on."""
    writing = (
        " It was writing a call to " + written[0] + "."
        if len(written) == 1
        else " It was writing calls to " + ", ".join(written[:-1]) + " and " + written[-1] + "."
        if written
        else ""
    )
    if not offered:
        return (
            "Your previous response was cut off at the output limit before it finished."
            + writing
            + " None of it was shown to anyone. Write a shorter, complete reply now."
        )
    adding = (
        f"add the rest with {EDIT_DOCUMENT_TOOL}"
        if EDIT_DOCUMENT_TOOL in offered
        else "add the rest in further edits"
    )
    return (
        "Your previous response was cut off at the output limit before it finished."
        + writing
        + " Nothing from it ran, and none of it was shown to anyone. Continue with a shorter "
        "response. Put long content in parts: create a document with its first part, then "
        + adding
        + "; build a long file in several smaller saves. Keep tool arguments compact, and do "
        "not repeat what is already done."
    )


def _offered_names(tools: Any) -> list[str]:
    names = []
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, Mapping) else None
        name = function.get("name") if isinstance(function, Mapping) else None
        if isinstance(name, str):
            names.append(name)
    return names


class CutOffReplies:
    """One turn's (or one helper's) handling of replies the output limit cut off.

    ``settle`` after every model call: False for a finished reply, which the loop handles as
    before; True for a cut-off one, already recorded with its note added, so the loop goes round
    again; past MAX_RECOVERIES it raises ``fail()``. ``output_tokens`` gives each call's allowance.
    """

    def __init__(
        self,
        store,
        owner: str,
        task_id: str,
        limits,
        *,
        fail: Callable[[], BaseException] = output_limit_failure,
        event: Mapping[str, Any] | None = None,
    ) -> None:
        self.store, self.owner, self.task_id, self.limits = store, owner, task_id, limits
        self.fail = fail
        self.event = dict(event or {})
        self.recoveries = 0
        self._raise_next = False
        self._raised = False

    def output_tokens(self, allowance: int, messages: list, tools: Any) -> int:
        """The output allowance for the call about to be sent with these messages and tools.

        The turn's own allowance, except right after the first cut-off when the model's
        catalogued ceiling is larger: then up to that ceiling, as far as the request leaves
        room in the window its context was fitted to (input budget plus output allowance).
        """
        if not self._raise_next:
            return allowance
        self._raise_next = False
        ceiling = getattr(self.limits, "output_ceiling", None)
        if type(ceiling) is not int or ceiling <= allowance:
            return allowance
        reserve = (
            _IMAGE_RESERVE_TOKENS
            if any(isinstance(m.get("content"), list) for m in messages if isinstance(m, dict))
            else 0
        )
        room = self.limits.context_tokens - estimate_request_tokens(messages, tools) - reserve
        raised = min(ceiling, allowance + max(0, room))
        if raised > allowance:
            self.store.event(
                self.owner,
                self.task_id,
                "output_allowance_raised",
                {**self.event, "output_tokens": raised, "allowance": allowance},
            )
        return raised

    def settle(
        self, call_id: str, response: Any, notes: list, *, tools: Any = None, limit_error=False
    ) -> bool:
        """Whether this call's reply was cut off; if so it is settled and the model is told.

        ``limit_error`` says the client raised LLMOutputLimitError with this response. The note
        goes on ``notes``: the messages the loop sends next.
        """
        if not limit_error and response_cut_off_by_output_limit(response) is None:
            return False
        usage = getattr(response, "usage", None)
        self.store.finish_call(call_id, "incomplete", asdict(usage) if usage else None)
        written = written_tool_names(response)
        self.recoveries += 1
        retry = self.recoveries <= MAX_RECOVERIES
        self.store.event(
            self.owner,
            self.task_id,
            "output_cut_off",
            {**self.event, "tools": written, "attempt": self.recoveries, "retry": retry},
        )
        if not retry:
            raise self.fail() from None
        if not self._raised:
            self._raised = self._raise_next = True
        notes.append({"role": "system", "content": cut_off_note(written, _offered_names(tools))})
        return True
