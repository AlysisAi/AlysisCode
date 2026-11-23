"""Where an owl turn's time went, from the task record the owls service returns.

A turn's wall time is split into parts that never overlap, so they add up to it:

- **model**: time inside model calls, by what each call was for (the ``purpose`` its
  ``model_started`` event names: the turn's own steps, the action reviewer, the reply checker,
  summaries, memory sorting, research helpers, …). Records from services older than these
  labels still name the reviewer's and the checker's calls through their own events; any
  other call counts as ``unlabelled``.
- **tools**: time inside tool steps (``tool_started`` to its ``tool_completed`` or
  ``tool_failed``), by kind of tool, without the model calls made inside a step (an action
  review runs inside the step it reviews).
- **service**: the rest of the turn: building requests, saving results, waiting for a lane.

Two more numbers come beside them: the seconds before the turn's first own model step (where
a summary of earlier chats or memory sorting would make the person wait) and the length of
the first browser or computer step (which includes waking the computer when it slept).

Like the rest of the benchmark this never imports the engine: the event kinds, purposes and
tool names below are the service's documented, fixed tokens.
"""

from __future__ import annotations

from collections import defaultdict

TERMINAL_EVENTS = ("completed", "failed", "cancelled", "interrupted")
TOOL_END_EVENTS = ("tool_completed", "tool_failed")
# Older services named only these calls, each in an event of its own carrying the call id.
LEGACY_PURPOSE_EVENTS = {
    "action_review_call": "action_review",
    "reply_review_started": "reply_check",
}
TURN = "turn"

# The kinds of tool a step's time is counted under.
WEB_TOOLS = frozenset({"web_search", "web_fetch", "research_helpers"})
COMPUTER_TOOLS = frozenset({"local_command", "request_computer_control", "list_local_computers"})
FILE_TOOLS = frozenset(
    {
        "list_files",
        "read_file",
        "save_file",
        "import_local_file",
        "export_local_file",
        "query_tables",
        "describe_document_format",
        "create_document",
        "inspect_document",
        "edit_document",
        "check_document",
        "create_image",
        "edit_image",
    }
)
TOOL_GROUPS = ("browser", "computer", "web", "files", "other")

# The model parts, in the order a report shows them.
PURPOSES = (
    "turn",
    "action_review",
    "reply_check",
    "history_summary",
    "progress_summary",
    "memory_index",
    "research_helper",
    "image",
    "computer_work",
    "work_compaction",
    "coding",
    "peer",
    "unlabelled",
)


def tool_group(name: str) -> str:
    if name.startswith("browser_"):
        return "browser"
    if name.startswith("computer_") or name in COMPUTER_TOOLS:
        return "computer"
    if name in WEB_TOOLS:
        return "web"
    if name in FILE_TOOLS:
        return "files"
    return "other"


def _at(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _purposes(events: list[dict]) -> dict[str, str]:
    """Each call id's purpose, from the events that name it."""
    found: dict[str, str] = {}
    for event in events:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        call_id = data.get("call_id")
        if not isinstance(call_id, str):
            continue
        kind = event.get("kind")
        if kind == "model_started" and isinstance(data.get("purpose"), str):
            found[call_id] = data["purpose"]
        elif kind in LEGACY_PURPOSE_EVENTS:
            found.setdefault(call_id, LEGACY_PURPOSE_EVENTS[kind])
    return found


def _tool_spans(events: list[dict], end: float) -> list[tuple[float, float, str]]:
    """(start, end, tool name) of each step; a step that never ended runs to the turn's end."""
    open_steps: dict[str, list[float]] = defaultdict(list)
    spans = []
    for event in events:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        at = _at(event.get("created_at"))
        name = data.get("name")
        if at is None or not isinstance(name, str):
            continue
        if event.get("kind") == "tool_started":
            open_steps[name].append(at)
        elif event.get("kind") in TOOL_END_EVENTS and open_steps[name]:
            spans.append((open_steps[name].pop(0), at, name))
    for name, starts in open_steps.items():
        spans.extend((start, end, name) for start in starts)
    return spans


def time_split(row: dict) -> dict | None:
    """The parts of one turn's time, in seconds, or None when the record has no clock."""
    events = [event for event in row.get("events") or [] if isinstance(event, dict)]
    times = [(event.get("kind"), _at(event.get("created_at"))) for event in events]
    times = [(kind, at) for kind, at in times if at is not None]
    queued = next((at for kind, at in times if kind == "queued"), None)
    start = next((at for kind, at in times if kind == "running"), None)
    end = next((at for kind, at in reversed(times) if kind in TERMINAL_EVENTS), None)
    if end is None and row.get("status") in TERMINAL_EVENTS:
        end = _at(row.get("updated_at"))
    if start is None or end is None or end < start:
        return None

    purposes = _purposes(events)
    calls = []
    for call in row.get("usage") or []:
        began, ended = _at(call.get("created_at")), _at(call.get("updated_at"))
        if began is None or ended is None or ended < began:
            continue
        purpose = purposes.get(call.get("id"), "unlabelled")
        calls.append((max(began, start), min(ended, end), purpose))
    calls = [call for call in calls if call[1] > call[0]]
    steps = [(max(a, start), min(b, end), name) for a, b, name in _tool_spans(events, end)]
    steps = [step for step in steps if step[1] > step[0]]

    model: dict[str, float] = defaultdict(float)
    tools: dict[str, float] = defaultdict(float)
    service = 0.0
    points = sorted(
        {
            start,
            end,
            *(t for a, b, _ in calls for t in (a, b)),
            *(t for a, b, _ in steps for t in (a, b)),
        }
    )
    for left, right in zip(points, points[1:], strict=False):
        width = right - left
        active = [purpose for a, b, purpose in calls if a < right and b > left]
        if active:
            # Calls side by side (research helpers): the segment is shared between their parts.
            distinct = set(active)
            for purpose in distinct:
                model[purpose] += width / len(distinct)
            continue
        running = [(a, name) for a, b, name in steps if a < right and b > left]
        if running:
            # The innermost (latest started) step owns the segment.
            tools[tool_group(max(running)[1])] += width
            continue
        service += width

    first_turn = min((a for a, _, purpose in calls if purpose == TURN), default=None)
    computer_steps = sorted(
        (a, b) for a, b, name in steps if tool_group(name) in ("browser", "computer")
    )
    return {
        "wall_s": round(end - start, 3),
        "queue_s": round(start - queued, 3) if queued is not None and start >= queued else None,
        "model_s": {name: round(model[name], 3) for name in PURPOSES if model.get(name)},
        "tools_s": {name: round(tools[name], 3) for name in TOOL_GROUPS if tools.get(name)},
        "service_s": round(service, 3),
        "before_first_step_s": round(first_turn - start, 3) if first_turn is not None else None,
        "first_computer_step_s": round(computer_steps[0][1] - computer_steps[0][0], 3)
        if computer_steps
        else None,
    }
