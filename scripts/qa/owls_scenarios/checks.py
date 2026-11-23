"""Checks: pure functions on a scenario run's frozen evidence.

Each check is ``{"kind": ..., ...}`` and answers ``(passed, detail)``. They read only the
evidence the runner froze (``run.freeze``), never the store or the model, so the same evidence
always gives the same verdicts and a check can be tested on hand-written evidence.

``turn`` names one turn by its step id (``"id"`` of the ``send`` that asked it), or
``"turn:N"`` for the N-th turn of the chat (1-based); left out, a check reads the first turn,
or every turn where it says so.

Expected values match *as a subset*: a dict matches when each of its keys matches the actual
value's (other keys are ignored), a list matches a list of the same length item by item, and
``{"$contains": "text"}`` matches a string (or a list of strings) that holds the text.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

Check = dict[str, Any]
Outcome = tuple[bool, str]


def matches(expected: Any, actual: Any) -> bool:
    """Whether ``actual`` holds ``expected`` (see the module's docstring)."""
    if isinstance(expected, dict):
        if set(expected) == {"$contains"}:
            needle = expected["$contains"]
            if isinstance(actual, str):
                return needle in actual
            if isinstance(actual, list):
                return any(isinstance(item, str) and needle in item for item in actual)
            return False
        if set(expected) == {"$exists"}:
            return (actual is not None) is bool(expected["$exists"])
        return isinstance(actual, dict) and all(
            matches(value, actual.get(key)) if value is not None else actual.get(key) is None
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(matches(e, a) for e, a in zip(expected, actual, strict=True))
        )
    return expected == actual


def _short(value: Any, limit: int = 300) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def turn_of(evidence: dict, ref: str | None) -> dict | None:
    turns = evidence.get("turns") or []
    if ref is None:
        return turns[0] if turns else None
    if ref.startswith("turn:") and ref[5:].isdigit():
        index = int(ref[5:]) - 1
        return turns[index] if 0 <= index < len(turns) else None
    return next((turn for turn in turns if turn.get("ref") == ref), None)


def _turns(evidence: dict, check: Check) -> list[dict] | None:
    """The turns a check reads: the one it names, else every turn."""
    if check.get("turn") is None:
        return list(evidence.get("turns") or [])
    turn = turn_of(evidence, check["turn"])
    return None if turn is None else [turn]


def _no_turn(check: Check) -> Outcome:
    return False, f"No turn {check.get('turn')!r} in this run."


def _bounds(count: int, check: Check) -> Outcome | None:
    """A failure when ``count`` is outside the check's ``equals``/``min``/``max``, else None."""
    if "equals" in check and count != check["equals"]:
        return False, f"{count}, expected exactly {check['equals']}."
    if "min" in check and count < check["min"]:
        return False, f"{count}, expected at least {check['min']}."
    if "max" in check and count > check["max"]:
        return False, f"{count}, expected at most {check['max']}."
    return None


# ------------------------------------------------------------------------------------- checks


def check_turn(evidence: dict, check: Check) -> Outcome:
    """A turn ended as expected: ``status``, ``failure_kind``, ``error_contains``."""
    turn = turn_of(evidence, check.get("turn"))
    if turn is None:
        return _no_turn(check)
    if "status" in check and turn["status"] != check["status"]:
        return False, f"Status {turn['status']!r}, expected {check['status']!r}."
    if "failure_kind" in check:
        kind = (turn.get("failure") or {}).get("kind")
        if kind != check["failure_kind"]:
            return False, f"Failure kind {kind!r}, expected {check['failure_kind']!r}."
    if "error_contains" in check and check["error_contains"] not in (turn.get("error") or ""):
        return False, f"Error {turn.get('error')!r} lacks {check['error_contains']!r}."
    return True, f"Turn {turn['ref']} ended {turn['status']}."


def _shown(evidence: dict, turns: list[dict]) -> list[str]:
    """Every text the person could see from these turns: replies, outputs, progress lines."""
    ids = {turn["task_id"] for turn in turns}
    texts = [reply["text"] for reply in evidence.get("replies") or [] if reply["task_id"] in ids]
    for turn in turns:
        texts.append(turn.get("output") or "")
        texts += [
            str(event["data"].get("text", ""))
            for event in turn.get("events") or []
            if event["kind"] == "progress_note"
        ]
    return [text for text in texts if text]


def check_reply(evidence: dict, check: Check) -> Outcome:
    """What was published: ``published`` (true/false), ``equals``, ``contains``, ``excludes``.

    ``excludes`` reads everything the person could see (``shown``: replies, the turn's output and
    its progress lines), so a withheld draft that leaked anywhere fails it.
    """
    turns = _turns(evidence, check)
    if turns is None:
        return _no_turn(check)
    ids = {turn["task_id"] for turn in turns}
    replies = [r["text"] for r in evidence.get("replies") or [] if r["task_id"] in ids]
    if "published" in check and bool(replies) is not check["published"]:
        said = "none" if not replies else _short(replies)
        return False, f"Published replies: {said}; expected published={check['published']}."
    if "equals" in check and replies != [check["equals"]]:
        return False, f"Published {_short(replies)}, expected exactly {_short(check['equals'])}."
    for text in check.get("contains") or []:
        if not any(text in reply for reply in replies):
            return False, f"No published reply contains {text!r}: {_short(replies)}."
    shown = _shown(evidence, turns)
    for text in check.get("excludes") or []:
        leaked = [item for item in shown if text in item]
        if leaked:
            return False, f"Shown text contains {text!r}: {_short(leaked)}."
    return True, f"{len(replies)} published repl{'y' if len(replies) == 1 else 'ies'} as expected."


def _calls(evidence: dict, check: Check) -> list[dict] | None:
    source = check.get("source", "run")
    calls = evidence.get("tool_runs" if source == "run" else "requested_calls") or []
    if check.get("turn") is None:
        return list(calls)
    turn = turn_of(evidence, check["turn"])
    if turn is None:
        return None
    return [call for call in calls if call.get("task_id") == turn["task_id"]]


def check_tools(evidence: dict, check: Check) -> Outcome:
    """Tools ran in this order, with these arguments (subset): ``expect`` is a list of ``{"name",
    "arguments"?}``; ``order`` is ``subsequence`` (default: in this order, others between),
    ``exact`` (these and nothing else) or ``prefix``. ``source``: ``run`` (default, what reached
    a tool) or ``requested`` (what the model asked for)."""
    calls = _calls(evidence, check)
    if calls is None:
        return _no_turn(check)
    expected = check.get("expect") or []
    order = check.get("order", "subsequence")

    def fits(want, call):
        return want["name"] == call["name"] and matches(
            want.get("arguments", {}), call.get("arguments")
        )

    names = [call["name"] for call in calls]
    if order in ("exact", "prefix"):
        if order == "exact" and len(calls) != len(expected):
            return False, f"Ran {names}, expected exactly {[w['name'] for w in expected]}."
        if len(calls) < len(expected):
            return False, f"Ran {names}, expected to start with {[w['name'] for w in expected]}."
        for index, want in enumerate(expected):
            if not fits(want, calls[index]):
                return (
                    False,
                    f"Call {index + 1} was {_short(calls[index])}, expected {_short(want)}.",
                )
        return True, f"Ran {names}."
    position = 0
    for want in expected:
        while position < len(calls) and not fits(want, calls[position]):
            position += 1
        if position == len(calls):
            return False, f"{_short(want)} not found in order in {names}."
        position += 1
    return True, f"Ran {names}."


def check_tool_not_run(evidence: dict, check: Check) -> Outcome:
    """A tool never reached its implementation (``name``), in a turn or in the whole run."""
    calls = _calls(evidence, check)
    if calls is None:
        return _no_turn(check)
    ran = [call for call in calls if call["name"] == check["name"]]
    if ran:
        return False, f"{check['name']} ran {len(ran)} time(s): {_short(ran)}."
    return True, f"{check['name']} never ran."


CARD_KINDS = {
    "action_review": "action_reviews",
    "proposal": "proposals",
    "question": "questions",
    "routine_suggestion": "routine_suggestions",
    "check_back": "check_backs",
    "app_need": "app_needs",
}


def check_card(evidence: dict, check: Check) -> Outcome:
    """A card or question waits for (or was raised to) the person: ``card`` is one of
    CARD_KINDS; ``fields`` (subset) picks the cards that count; ``present`` (default true) or
    ``equals``/``min``/``max`` bound how many."""
    turns = _turns(evidence, check)
    if turns is None:
        return _no_turn(check)
    key = CARD_KINDS[check["card"]]
    cards = [card for turn in turns for card in (turn.get("cards") or {}).get(key) or []]
    found = [card for card in cards if matches(check.get("fields") or {}, card)]
    if any(bound in check for bound in ("equals", "min", "max")):
        failed = _bounds(len(found), check)
        if failed:
            return False, f"{check['card']} cards: {failed[1]} Cards: {_short(cards)}"
    elif bool(found) is not check.get("present", True):
        return (
            False,
            f"{check['card']} cards matching {_short(check.get('fields'))}: {_short(cards)}.",
        )
    return True, f"{len(found)} {check['card']} card(s) as expected."


def check_event(evidence: dict, check: Check) -> Outcome:
    """An engine event (``event``) with ``data`` (subset) is ``present`` (default true) or absent,
    or occurs ``equals``/``min``/``max`` times, in a turn or the whole run."""
    turns = _turns(evidence, check)
    if turns is None:
        return _no_turn(check)
    found = [
        event
        for turn in turns
        for event in turn.get("events") or []
        if event["kind"] == check["event"] and matches(check.get("data") or {}, event["data"])
    ]
    if any(bound in check for bound in ("equals", "min", "max")):
        failed = _bounds(len(found), check)
        if failed:
            return False, f"{check['event']} events: {failed[1]}"
    elif bool(found) is not check.get("present", True):
        kinds = [e["kind"] for turn in turns for e in turn.get("events") or []]
        return False, f"{check['event']} present={bool(found)}; events were {kinds}."
    return True, f"{len(found)} {check['event']} event(s) as expected."


COUNTABLE = {
    "model_calls": "model_calls",
    "tool_runs": "tool_runs",
    "requested_calls": "requested_calls",
    "replies": "replies",
    "turns": "turns",
}


def check_count(evidence: dict, check: Check) -> Outcome:
    """How many ``of`` (model_calls, tool_runs, requested_calls, replies, turns) match ``where``
    (subset): ``equals``, ``min``, ``max``."""
    items = evidence.get(COUNTABLE[check["of"]]) or []
    found = [item for item in items if matches(check.get("where") or {}, item)]
    failed = _bounds(len(found), check)
    if failed:
        return False, f"{check['of']} where {_short(check.get('where'))}: {failed[1]}"
    return True, f"{len(found)} {check['of']} as expected."


def _leaves(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _leaves(item)]
    if isinstance(value, list):
        return [text for item in value for text in _leaves(item)]
    return []


def _texts(messages: list[dict]) -> list[str]:
    """Every text a request holds: each message's content as sent and, where that content is
    JSON (a tool result), each of its strings; tool call arguments likewise."""
    texts: list[str] = []
    for message in messages:
        content = message.get("content")
        texts += _leaves(content)
        if isinstance(content, str) and content[:1] in "{[":
            try:
                texts += _leaves(json.loads(content))
            except ValueError:
                pass
        for call in message.get("tool_calls") or []:
            arguments = (call.get("function") or {}).get("arguments")
            texts += _leaves(arguments)
            if isinstance(arguments, str):
                try:
                    texts += _leaves(json.loads(arguments))
                except ValueError:
                    pass
    return texts


def check_request(evidence: dict, check: Check) -> Outcome:
    """What the model was sent on owl call ``owl_call`` (1-based, the whole run): ``tail`` (the
    last messages, subset each, e.g. roles), ``contains``/``excludes`` (text in a message, in a
    tool result's strings or in a call's arguments), ``offered``/``not_offered`` (tool names)."""
    calls = [c for c in evidence.get("model_calls") or [] if c["channel"] == "owl"]
    number = check["owl_call"]
    if not 1 <= number <= len(calls):
        return False, f"There were {len(calls)} owl calls; no call {number}."
    call = calls[number - 1]
    messages = call["messages"]
    tail = check.get("tail")
    if tail is not None:
        actual = messages[-len(tail) :] if tail else []
        if not matches(tail, actual):
            shown = [{k: m.get(k) for k in ("role", "content")} for m in actual]
            return False, f"Owl call {number} ended with {_short(shown, 600)}."
    texts = _texts(messages)
    for needle in check.get("contains") or []:
        if not any(needle in text for text in texts):
            return False, f"Owl call {number} was not sent {needle!r}."
    for needle in check.get("excludes") or []:
        if any(needle in text for text in texts):
            return False, f"Owl call {number} was sent {needle!r}."
    for name in check.get("offered") or []:
        if name not in call["tools"]:
            return False, f"Owl call {number} was not offered {name}: {call['tools']}."
    for name in check.get("not_offered") or []:
        if name in call["tools"]:
            return False, f"Owl call {number} was offered {name}."
    return True, f"Owl call {number} was sent what was expected."


def check_routine(evidence: dict, check: Check) -> Outcome:
    """A routine (by ``name``, else the only one) has these fields (subset): ``state``,
    ``pause_code``, ``nothing_new`` (whether its last run checked and found nothing)..."""
    routines = evidence.get("routines") or []
    if "name" in check:
        routines = [r for r in routines if r["name"] == check["name"]]
    if len(routines) != 1:
        return False, f"Expected one routine, found {_short(evidence.get('routines'))}."
    expected = {k: v for k, v in check.items() if k not in ("kind", "name", "why")}
    if not matches(expected, routines[0]):
        return False, f"Routine is {_short(routines[0])}, expected {_short(expected)}."
    return True, f"Routine {routines[0]['name']!r} is as expected."


def _path(evidence: dict, path: str) -> Any:
    value: Any = evidence
    for key in path.split("."):
        if isinstance(value, dict):
            value = value.get(key)
        elif isinstance(value, list) and key.isdigit():
            value = value[int(key)] if int(key) < len(value) else None
        else:
            return None
    return value


def check_path(evidence: dict, check: Check) -> Outcome:
    """Any value of the evidence by dotted ``path``: ``equals`` / ``matches`` (subset),
    ``contains`` / ``excludes`` (an item of a list, or text in a string)."""
    value = _path(evidence, check["path"])
    if "equals" in check and value != check["equals"]:
        return False, f"{check['path']} is {_short(value)}, expected {_short(check['equals'])}."
    if "matches" in check and not matches(check["matches"], value):
        return False, f"{check['path']} is {_short(value)}, expected {_short(check['matches'])}."
    if "contains" in check and check["contains"] not in (value or []):
        return False, f"{check['path']} is {_short(value)}, lacks {_short(check['contains'])}."
    if "excludes" in check and check["excludes"] in (value or []):
        return False, f"{check['path']} is {_short(value)}, holds {_short(check['excludes'])}."
    return True, f"{check['path']} is {_short(value, 120)}."


CHECKS: dict[str, Callable[[dict, Check], Outcome]] = {
    "turn": check_turn,
    "reply": check_reply,
    "tools": check_tools,
    "tool_not_run": check_tool_not_run,
    "card": check_card,
    "event": check_event,
    "count": check_count,
    "request": check_request,
    "routine": check_routine,
    "path": check_path,
}
REQUIRED = {
    "tools": ("expect",),
    "tool_not_run": ("name",),
    "card": ("card",),
    "event": ("event",),
    "count": ("of",),
    "request": ("owl_call",),
    "path": ("path",),
}


def problems(check: Check) -> list[str]:
    """What is wrong with a check's own shape (empty when it can run)."""
    kind = check.get("kind")
    if kind not in CHECKS:
        return [f"Unknown check kind {kind!r}; known: {sorted(CHECKS)}."]
    found = [f"{kind} check needs {key!r}." for key in REQUIRED.get(kind, ()) if key not in check]
    if kind == "card" and check.get("card") not in CARD_KINDS:
        found.append(f"Unknown card {check.get('card')!r}; known: {sorted(CARD_KINDS)}.")
    if kind == "count" and check.get("of") not in COUNTABLE:
        found.append(f"Unknown count of {check.get('of')!r}; known: {sorted(COUNTABLE)}.")
    return found


def evaluate(evidence: dict, checks: list[Check]) -> list[dict]:
    """Every check's verdict, in order: ``{"check", "passed", "detail"}``."""
    verdicts = []
    for check in checks:
        wrong = problems(check)
        if wrong:
            passed, detail = False, " ".join(wrong)
        else:
            passed, detail = CHECKS[check["kind"]](evidence, check)
        verdicts.append({"check": check, "passed": passed, "detail": detail})
    return verdicts
