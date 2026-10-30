"""Typed provider goal reports are separate from host execution and verification.

Missing/malformed reports remain incomplete; no keyword interpretation of prose.
The host still owns all execution, itinerary, file and independent verification gates.
A report is the whole message as one JSON object, or one entire ```json (or unlabelled)
block holding it. Nothing is extracted from prose, and old outcome words are not aliases.
"""

from __future__ import annotations

import json
import re
from typing import Any

OUTCOMES = frozenset({"completed_unverified", "incomplete", "blocked"})
MAX_REPORT_BYTES = 65_536
_FENCE = re.compile(r"```(?:json)?[ \t]*\r?\n(.*)\n[ \t]*```", re.DOTALL)
_JSON_WHITESPACE = " \t\r\n"
_SURROGATE = re.compile("[\ud800-\udfff]")


class _RejectedReport(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise _RejectedReport
    return dict(pairs)


def _no_constant(_name: str) -> Any:
    raise _RejectedReport


def computer_use_completion_report(text: str) -> tuple[str, str, bool]:
    undeclared = ("incomplete", text, False)
    # Every character is at least one UTF-8 byte, so this bounds the encoding below.
    if not isinstance(text, str) or len(text) > MAX_REPORT_BYTES:
        return undeclared
    if _SURROGATE.search(text) or len(text.encode("utf-8")) > MAX_REPORT_BYTES:
        return undeclared
    body = text.strip(_JSON_WHITESPACE)
    if body.startswith("```"):
        fenced = _FENCE.fullmatch(body)
        if not fenced:
            return undeclared
        body = fenced.group(1)
    try:
        value: Any = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_no_constant)
    except (ValueError, TypeError, RecursionError):
        return undeclared
    if (
        not isinstance(value, dict)
        or set(value) != {"outcome", "summary"}
        or not isinstance(value.get("outcome"), str)
        or value["outcome"] not in OUTCOMES
        or not isinstance(value.get("summary"), str)
        or not value["summary"].strip()
        or _SURROGATE.search(value["summary"])
    ):
        return undeclared
    return value["outcome"], value["summary"].strip(), True
