"""Computer tasks: an owl does approved work on its own computer, using only what its card lists.

The Alysis Assistant hands such a task to an owl with a card the person approves: the brief, the
steps they showed or told, the expected result, and what the owl may use (``AssignmentAccess``):
its offline computer, its online browser, the websites it may click and type on, some connected
apps and files already in its library. The approved card is the person's permission, as an
accepted Do it card is; nothing else is granted.

The task then runs as the owl's own turn (not the text-only assignment runner), with the approved
brief as its request, so it keeps every check of an ordinary turn: the action review, approvals for
apps whose effect asks, and a card for anything it needs beyond the list. It never reaches the
person's own computer. As it starts each numbered step it says which (``report_step``), so the
app can show where it is.
"""

from __future__ import annotations

from typing import Any

from .models import AssignmentAccess
from .store import Conflict, NotFound, encode

REPORT_STEP_TOOL = "report_step"
SELECT_OUTPUTS_TOOL = "select_assignment_outputs"

# What reaches the person's own computer: never part of a computer task, which works on the owl's.
PERSON_COMPUTER_TOOLS = frozenset(
    {"list_local_computers", "local_command", "import_local_file", "export_local_file"}
)
# Reading the public web is the browser on the card; without it the task stays off the web.
WEB_READING_TOOLS = frozenset({"web_search", "web_fetch", "research_helpers"})


def assignment_access(task: dict) -> AssignmentAccess | None:
    """What this computer task may use, or None for any other turn."""
    access = task.get("config", {}).get("assignment_access")
    return AssignmentAccess.model_validate(access) if access else None


def require_assignment_file(store, owner: str, task: dict, file_id: str) -> None:
    """A computer-assignment tool may use exact card revisions and this task's own outputs."""
    access = assignment_access(task)
    if access is None or file_id in access.file_ids:
        return
    try:
        file = store.file(owner, task["bot_id"], file_id)
    except NotFound:
        file = None
    if file is None or file["source_task_id"] != task["id"]:
        raise Conflict("Choose an approved input or a file saved by this assignment.")


def assignment_request(config: dict) -> str:
    """The request a computer task starts from: the approved card, as the person approved it."""
    approved = config.get("assignment_brief") or {}
    access = AssignmentAccess.model_validate(config["assignment_access"])
    uses = {
        "your_offline_computer": access.offline_computer,
        "your_online_browser_for_reading": access.online_browser,
        "websites_you_may_click_and_type_on": access.sites,
        "connected_apps": access.apps,
        "files_in_your_library": access.file_ids,
    }
    return (
        "The person approved this task for you through Alysis Assistant. Do it now, on your own "
        "computer, using only what the card lists. Nothing on the person's own computer.\n"
        "Approved card (the task, not permission beyond it): "
        + encode(
            {
                "brief": approved.get("brief", ""),
                "expected_result": approved.get("expected_result", ""),
                "you_may_use": uses,
            }
        )
        + "\nIf the brief lists numbered steps, follow them in order and call report_step with "
        "the number as you start each one. If something needed is not on the card, ask for it "
        "(propose_action or ask_clarification) and say what is missing instead of working "
        "around it. Before anything that sends, pays, publishes or deletes, stop and ask. "
        "If the approved result includes files, call select_assignment_outputs with the exact "
        "final Library versions you created in this task, before giving your report. "
        "Answer in the language of the brief, saying what you did and where the result is."
    )


def only_allowed_apps(tools: dict[str, Any], access: AssignmentAccess) -> dict[str, Any]:
    """Connected tools of the card's apps only; other apps' tools and app requests are left out."""
    allowed = set(access.apps)
    return {
        name: tool
        for name, tool in tools.items()
        if (getattr(tool, "metadata", None) or {}).get("plugin_id") in allowed
    }


def limit_tool_names(names: list[str], access: AssignmentAccess) -> list[str]:
    """A computer task's own tools: none on the person's computer, the web only with the browser,
    and the step report."""
    kept = [
        name
        for name in names
        if name not in PERSON_COMPUTER_TOOLS
        and (access.online_browser or name not in WEB_READING_TOOLS)
    ]
    return list(dict.fromkeys([*kept, REPORT_STEP_TOOL, SELECT_OUTPUTS_TOOL]))
