"""What a model is told when a tool call of its was not run.

Plain ``ValueError`` text is never shown to a model, because some carries the call's raw
arguments; these errors are authored, carry no argument values, and say what to do instead.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import ValidationError

# A tool name is the model's own text: shown on one line, and never at length.
SHOWN_NAME_CHARACTERS = 80

INVALID_ARGUMENTS = "Invalid tool arguments."
# Pydantic's error types for a value longer than its field allows: text, and lists.
_TOO_LONG = {"string_too_long": "characters", "too_long": "items"}


def invalid_arguments(exc: ValidationError) -> dict:
    """What the model is told when its arguments failed the tool's check: nothing ran.

    Field feedback comes without the values sent. A value longer than its field allows says
    so in plain words, with the limit, and how to send it again: shorter, or in parts.
    """
    limits = []
    for error in exc.errors(include_input=False, include_url=False):
        unit = _TOO_LONG.get(error["type"])
        maximum = (error.get("ctx") or {}).get("max_length")
        if unit and type(maximum) is int:
            field = ".".join(str(part) for part in error["loc"]) or "the input"
            limits.append({"field": field, "max_length": maximum, "unit": unit})
    result: dict = {
        "error": INVALID_ARGUMENTS,
        "executed": False,
        "validation_errors": exc.errors(
            include_input=False, include_context=False, include_url=False
        )[:10],
    }
    if limits:
        sizes = "; ".join(
            f"{item['field']}: at most {item['max_length']:,} {item['unit']}" for item in limits
        )
        result["error"] = (
            f"Nothing was done: part of this call is longer than the tool takes ({sizes}). "
            "Send it again shorter, or split it across several calls (a long text as several "
            "smaller files or parts)."
        )
        result["limits"] = limits
    return result


class ToolNotOffered(Exception):
    """A call to a tool this step does not offer: nothing was run.

    Each step offers its own tools (owl_toolset.py, plus the computers and apps on now), so a
    tool seen earlier in the conversation can be gone. Being told the arguments were wrong
    would send the model fixing arguments that were never the problem.
    """

    def __init__(self, name: object, offered: Iterable[str]) -> None:
        self.offered = list(dict.fromkeys(offered))
        shown = " ".join(str(name).split())[:SHOWN_NAME_CHARACTERS] or "(no name)"
        super().__init__(
            f"The tool {shown} is not offered now, so nothing was run and its arguments were "
            "not checked. Tools you can call now: " + ", ".join(self.offered) + "."
            if self.offered
            else f"The tool {shown} is not offered now, so nothing was run. No tools are "
            "offered now: reply in plain text."
        )
