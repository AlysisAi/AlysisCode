"""Which tools an owl's turn is offered.

A turn is offered the tools it can use: what it may do depends on its kind (answering the
person, a routine's run, an activation's review, background work, a room) and on what its owl,
service and model have (memory, files, a coding workspace, a browser, other owls, web lookup,
tool calling). It is never decided from the words of the message. While those stay the same the
list stays the same, byte for byte, so a provider can keep caching it from turn to turn.

Not every turn can list every tool directly. Such a turn lists some and reaches the rest through
two more: ``tool_details`` returns a hidden tool's full description and inputs, ``use_tool`` runs
it. A call through ``use_tool`` becomes the hidden tool's own call before anything runs
(``Offer.unwrap``), so it takes exactly the path, checks, approvals and receipts of a direct call.

How a turn's tools are offered (its mode) is decided from measures of the list itself, never from
the model's name or the message's words:

- ``full``: every tool directly.
- ``apps_by_lookup``: the owl's own tools directly; the tools of connected apps, which grow with
  every app the person connects, by look-up. Taken when the full list has more tools than one
  request can safely carry, its schemas take more than a share of the model's declared context
  window, or, on a route with an admission bound of its own, the request's standing part (the
  guidance and the turn's own state, no conversation) with that list takes more than a share of
  the bound.
- ``everyday``: the everyday tools directly, every other tool by look-up. Taken when the model's
  declared context window is small, and when even without the apps' tools the list breaks the
  tool count or the route's share.

A turn never goes back to a longer list (``ToolRoom``): once a tool was reached by look-up, it is
reached that way until the turn ends. Each turn records its mode and measures in a
``tools_offered`` event.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

from pydantic import Field

from ..llm.types import ToolCall
from ..request_estimation import estimate_tool_schema_tokens
from .models import Input
from .owl_documents import DOCUMENT_TOOLS
from .owl_images import IMAGE_TOOLS
from .owl_models import context_window
from .owl_tables import TABLE_TOOLS
from .owl_tools import ROOM_TURN_LEFT_OUT, ROUTINE_TOOLS
from .research_helpers import RESEARCH_TOOL
from .step_effects import NOTHING_TO_REPORT
from .store import NotFound, encode

# Below this declared context window (in tokens) a model counts as small. The full tool list of
# an owl with every feature is about 36,000 tokens (63 owl tools), 49,000 with its browser (89),
# and the standing guidance about 5,000 more: below 100,000 tokens that would take over a third
# of everything the model can read, before the conversation even starts.
SMALL_CONTEXT_WINDOW_TOKENS = 100_000

# At most this many tools are listed directly in one request. Endpoints that limit the tools of a
# request refuse more than 128 ("Invalid 'tools': array too long. Expected an array with maximum
# length 128"); the engine keeps no provider's own limit to read instead. Three quarters of 128
# keeps a margin under it, and still lists an owl's own tools with every feature and its browser
# (89) plus a few tools of connected apps.
MAX_DIRECT_TOOLS = 96

# Connected apps' tools go behind look-up when every tool's schema together would take more than
# this share of the model's declared context window, leaving three quarters for the guidance, the
# conversation, files and the answer. A fifth would hide even one small app's tools from an owl
# with memory and files on a 128,000-token window (the registry's default for a model it doesn't
# know), where that owl's own tools already take 22%. The owl's own tools are a bounded list that
# SMALL_CONTEXT_WINDOW_TOKENS sizes, so this share alone never hides them.
TOOL_SCHEMA_WINDOW_SHARE = 0.25

# On a route with an admission bound of its own (the hosted route counts request bytes:
# llm/hosted_request_limits.py), connected apps' tools go behind look-up when the standing request
# (guidance and the turn's own state, without the conversation) with the full list takes more
# than this share of the bound, and the owl's own tools too when it still does without them. Half
# stays for the conversation, tool results and images. Measured: an owl with every feature and its
# browser takes about 36% of a 1,000,000 bound (unchanged), and about 140% of a 262,144 bound,
# where it gets the everyday tools. 35% would sit on the first owl's own 36%, so it would change
# mode as its instructions or memories grew by a few lines.
ROUTE_BASE_SHARE = 0.5

# How a turn's tools are offered, from the longest list to the shortest.
FULL = "full"
APPS_BY_LOOKUP = "apps_by_lookup"
EVERYDAY = "everyday"
MODES = (FULL, APPS_BY_LOOKUP, EVERYDAY)

# Why a turn's tools were not all listed directly (an Offer's reasons).
SMALL_WINDOW = "small_window"
TOO_MANY_TOOLS = "tool_count"
SCHEMAS_OVER_SHARE = "schema_share"
ROUTE_OVER_SHARE = "route_share"

LOOKUP_TOOL = "tool_details"
CALL_THROUGH_TOOL = "use_tool"
COMPACT_TOOLS = frozenset((LOOKUP_TOOL, CALL_THROUGH_TOOL))

# Offered directly to a small model: reading the web and pages, the person's files and memory,
# asking and approvals, and reading back a long earlier result (BotRuntime._fit_context moves
# long results out of the conversation only while read_tool_result is offered).
EVERYDAY_TOOLS = frozenset(
    {
        "web_search",
        "web_fetch",
        "browser_navigate",
        "browser_observe",
        "read_tool_result",
        "ask_clarification",
        "propose_action",
        "withdraw_action",
        "list_files",
        "read_file",
        "save_file",
        "list_memories",
        "read_memory",
        "propose_memory",
        # Reading a playbook the task state lists, before a job it fits.
        "read_playbook",
        # Coming back later instead of waiting in the turn, which the guidance names.
        "check_back",
        # How a routine's run that found nothing new ends (offered only in routine runs).
        NOTHING_TO_REPORT,
    }
)

PEER_TOOLS = (
    "list_peer_bots",
    "request_peer",
    "read_peer_exchange",
    "update_peer_exchange",
    "stop_peer_exchange",
)
CODING_TOOLS = ("coding_task", "read_coding_result", "update_coding_task", "stop_coding_task")
FILE_TOOLS = ("list_files", "read_file", "save_file", "import_local_file", "export_local_file")
MEMORY_TOOLS = (
    "propose_memory",
    "list_memories",
    "read_memory",
    "memory_history",
    "memory_source_read",
    "search_history",
    "read_message",
    "read_working_note",
    "search_working_notes",
    "save_working_note",
    "consolidate_working_notes",
)
# An activation's review phase reads and proposes; it never acts.
ACTIVATION_REVIEW_TOOLS = frozenset(
    {
        "read_tool_result",
        "list_schedules",
        "list_triggers",
        "propose_action",
        "withdraw_action",
        "list_files",
        "read_file",
        "query_tables",
        "list_memories",
        "read_memory",
        "memory_history",
        "memory_source_read",
        "search_history",
        "read_message",
        "read_working_note",
        "search_working_notes",
        "read_coding_result",
        "read_playbook",
    }
)
# Background work is already running on its own: it starts no other work and makes no offers.
NOT_IN_BACKGROUND = frozenset(
    {
        "continue_in_background",
        "coding_task",
        "update_coding_task",
        "stop_coding_task",
        # A bot runs one background turn at a time, and this is it.
        "read_background_task",
        "update_background_task",
        "stop_background_task",
        "propose_schedule",
        "propose_trigger",
        # Research helpers report to a turn that waits for them, in the person's chat.
        RESEARCH_TOOL,
    }
)


@dataclass(frozen=True)
class Turn:
    """What one turn is and what its owl and service have; every field comes from stored state."""

    # Answering the person's own chat message: not background work, an activation, a routine's
    # run, work carried on after a coding job or a peer's report.
    answers_person: bool
    background: bool = False
    activation: bool = False
    scheduled: bool = False
    room: bool = False
    first_hello: bool = False
    regenerated: bool = False
    skipped_question: bool = False
    # The person's own message (regenerate.persons_request), and a turn the owl's own check-back
    # started (check_backs.py): only these may set a check-back. A check-back turn carries the
    # person's request on with nobody's new words, so it sets up nothing on their behalf.
    own_message: bool = False
    checking_back: bool = False
    # Service features.
    web_lookup: bool = False
    # The model the turn thinks with can call tools (its client's declared capability), which
    # research helpers need for their own searches and reads.
    tool_calling: bool = False
    browser: bool = False
    computer_handoff: bool = False
    # The owl's features.
    files: bool = False
    # The engine can make Word, Excel, PowerPoint or PDF files itself (owl_documents.py).
    documents: bool = False
    # The model connection the owl thinks with declares image generation (owl_images.py).
    images: bool = False
    memory: bool = False
    coding: bool = False
    other_owls: bool = False
    about_me: bool = False
    # The owl has playbooks (playbooks.py), and this turn's reply goes through the reply checker:
    # only a checked run can keep one it writes.
    playbooks: bool = False
    reply_checked: bool = False


def tool_names(turn: Turn) -> list[str]:
    """The owl tools a turn is offered, in a fixed order."""
    names = [
        *(["web_search", "web_fetch"] if turn.web_lookup else []),
        *([RESEARCH_TOOL] if turn.web_lookup and turn.tool_calling else []),
        "list_local_computers",
        "local_command",
        *(["request_computer_control"] if turn.computer_handoff else []),
        "read_tool_result",
        # Form values saved from a browser page; there are none without a browser.
        *(["read_browser_draft"] if turn.browser else []),
        "ask_clarification",
        "continue_in_background",
        "read_background_task",
        "update_background_task",
        "stop_background_task",
        "list_schedules",
        "propose_action",
        "withdraw_action",
        "list_triggers",
        "propose_trigger",
    ]
    if turn.answers_person and not turn.checking_back:
        names += sorted(ROUTINE_TOOLS)
        names += ["answer_clarification", "propose_setup"]
        # A routine is offered for a need the person shows, while answering them: not in a
        # first hello, nor in another answer to a message already answered.
        if not turn.first_hello and not turn.regenerated:
            names.append("propose_schedule")
        if turn.other_owls:
            names += PEER_TOOLS
        # Only the person's own message can state a fact about them.
        if turn.about_me and not turn.first_hello and not turn.skipped_question:
            names.append("suggest_about_me")
    if turn.answers_person and (turn.own_message or turn.checking_back):
        names.append("check_back")
    if turn.room:
        names = [name for name in names if name not in ROOM_TURN_LEFT_OUT]
    if turn.coding:
        names += CODING_TOOLS
    if turn.files:
        names += FILE_TOOLS
        names += TABLE_TOOLS
        if turn.documents:
            names += DOCUMENT_TOOLS
        if turn.images:
            names += IMAGE_TOOLS
    if turn.memory:
        names += MEMORY_TOOLS
        if turn.playbooks:
            names.append("read_playbook")
        # A first hello does no job to write down, and a room leaves it to the owl's own chat.
        if turn.reply_checked and not turn.first_hello and not turn.room:
            names.append("save_playbook")
    if turn.activation:
        names = [name for name in names if name in ACTIVATION_REVIEW_TOOLS]
    if turn.scheduled:
        # Nobody is there to answer a routine run's question. A run that found nothing new
        # may end without a reply.
        names = [name for name in names if name != "ask_clarification"]
        names.append(NOTHING_TO_REPORT)
    if turn.background:
        names = [name for name in names if name not in NOT_IN_BACKGROUND]
    return names


def declared_context_window(client: Any) -> int:
    """The context window, in tokens, the model registry declares for this client's model."""
    return context_window(
        SimpleNamespace(
            model=str(getattr(client, "model", "") or ""),
            base_url=str(getattr(client, "base_url", "") or ""),
        )
    )


class ToolRequest(Input):
    name: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class Measures:
    """What a full tool list takes: how many tools, their schemas' estimated tokens, and the share
    of its route's admission bound the standing request with them takes (0.0 without a bound)."""

    tools: int
    schema_tokens: int
    route_share: float


@dataclass(frozen=True)
class Offer:
    """The tool schemas sent with one model call, the tools reachable only through them, and how
    and why they were chosen."""

    schemas: list[dict]
    hidden: dict[str, dict] = field(default_factory=dict)
    mode: str = FULL
    reasons: tuple[str, ...] = ()
    measures: Measures | None = None

    def unwrap(self, calls: list[ToolCall]) -> list[ToolCall]:
        """Each use_tool call naming a hidden tool, as that tool's own call; others unchanged."""
        return [self._target(call) or call for call in calls]

    def _target(self, call: ToolCall) -> ToolCall | None:
        if call.name != CALL_THROUGH_TOOL or not self.hidden:
            return None
        name, arguments = call.arguments.get("name"), call.arguments.get("arguments", {})
        if name not in self.hidden or not isinstance(arguments, dict):
            return None
        return replace(call, name=name, arguments=arguments)

    def answer(self, call: ToolCall) -> dict:
        """The result of a tool_details call, or why a use_tool call could not be run."""
        request = ToolRequest.model_validate(call.arguments)
        if request.name not in self.hidden:
            raise NotFound(
                f"{request.name} is not one of the tools reached through {call.name} now. "
                "Use one of: " + ", ".join(self.hidden) + "."
            )
        # A valid use_tool call never gets here: unwrap made it the tool's own call.
        return self.hidden[request.name]["function"]


def offer(
    schemas: list[dict],
    *,
    context_window: int,
    apps: Mapping[str, str] | None = None,
    route_share: Callable[[list[dict]], float] | None = None,
    at_least: str = FULL,
) -> Offer:
    """What one model call is offered, and in which mode (see the module's docstring).

    ``apps``: the tools of connected apps, each with the line the look-up lists it by; they go
    behind look-up first. ``route_share``: the share of its route's admission bound the standing
    request takes with a given tool list (None: the route has no such bound). ``at_least``: the
    most compact mode this turn has used; a turn never goes back to a longer list.
    """
    lines = dict(apps or {})
    measures = _measure(schemas, route_share)
    reasons = tuple(
        reason
        for reason, over in (
            (SMALL_WINDOW, context_window < SMALL_CONTEXT_WINDOW_TOKENS),
            (TOO_MANY_TOOLS, measures.tools > MAX_DIRECT_TOOLS),
            (
                SCHEMAS_OVER_SHARE,
                measures.schema_tokens > context_window * TOOL_SCHEMA_WINDOW_SHARE,
            ),
            (ROUTE_OVER_SHARE, measures.route_share > ROUTE_BASE_SHARE),
        )
        if over
    )
    floor = MODES.index(at_least)
    everyday = {_name(s) for s in schemas if _name(s) not in EVERYDAY_TOOLS}
    if SMALL_WINDOW in reasons or floor >= MODES.index(EVERYDAY):
        return _behind_lookup(schemas, everyday, lines, EVERYDAY, reasons, measures)
    of_apps = {_name(s) for s in schemas if _name(s) in lines}
    if of_apps and (reasons or floor >= MODES.index(APPS_BY_LOOKUP)):
        by_app = _behind_lookup(schemas, of_apps, lines, APPS_BY_LOOKUP, reasons, measures)
        if not _over_limits(by_app.schemas, route_share):
            return by_app
        return _behind_lookup(schemas, everyday, lines, EVERYDAY, reasons, measures)
    if TOO_MANY_TOOLS in reasons or ROUTE_OVER_SHARE in reasons:
        return _behind_lookup(schemas, everyday, lines, EVERYDAY, reasons, measures)
    return Offer(schemas, mode=FULL, reasons=reasons, measures=measures)


def _name(schema: dict) -> str:
    return schema["function"]["name"]


def _measure(schemas: list[dict], route_share: Callable[[list[dict]], float] | None) -> Measures:
    return Measures(
        tools=len(schemas),
        schema_tokens=estimate_tool_schema_tokens(schemas),
        route_share=_share(schemas, route_share),
    )


def _share(schemas: list[dict], route_share: Callable[[list[dict]], float] | None) -> float:
    if route_share is None:
        return 0.0
    share = route_share(schemas)
    if isinstance(share, bool) or not isinstance(share, int | float) or not math.isfinite(share):
        raise ValueError("The route's measure of a request's share of its bound is not a number.")
    if share < 0:
        raise ValueError("The route's measure of a request's share of its bound is below zero.")
    return float(share)


def _over_limits(schemas: list[dict], route_share: Callable[[list[dict]], float] | None) -> bool:
    """Whether a list still breaks what one request may carry: the tool count or the route's
    share. (The window share only decides whether apps' tools go behind look-up.)"""
    return len(schemas) > MAX_DIRECT_TOOLS or _share(schemas, route_share) > ROUTE_BASE_SHARE


def _behind_lookup(
    schemas: list[dict],
    hide: set[str],
    lines: Mapping[str, str],
    mode: str,
    reasons: tuple[str, ...],
    measures: Measures,
) -> Offer:
    """``schemas`` with the tools named in ``hide`` reached through tool_details and use_tool."""
    hidden = {_name(s): s for s in schemas if _name(s) in hide}
    if not hidden:
        return Offer(schemas, mode=FULL, reasons=reasons, measures=measures)
    direct = [s for s in schemas if _name(s) not in hide]
    return Offer(
        [*direct, *_compact_schemas(hidden, lines)],
        hidden,
        mode=mode,
        reasons=reasons,
        measures=measures,
    )


def app_tools(tools: Mapping[str, Any]) -> dict[str, str]:
    """The tools of connected apps among ``tools`` (each with a ``plugin_id``), and the line the
    look-up lists each by: the app's name and the first sentence of the app's own description
    of the tool, which the tool's full description puts after notes shared by all of its tools."""
    lines = {}
    for name, tool in tools.items():
        metadata = getattr(tool, "metadata", None) or {}
        if not metadata.get("plugin_id"):
            continue
        own = _summary(str(metadata.get("tool_description") or getattr(tool, "description", "")))
        app = str(metadata.get("plugin_name") or "").strip()
        lines[name] = _summary(f"{app}: {own}" if app else own)
    return lines


def standing_messages(messages: list[dict]) -> list[dict]:
    """The standing part of a turn's request: its system messages (the guidance, the turn's own
    state, notes about the present) without the conversation or its summary, and without the
    fields only the service reads."""
    return [
        {key: value for key, value in message.items() if not key.startswith("_")}
        for message in messages
        if message.get("role") == "system"
        and "_history_task_id" not in message
        and "_bot_summary" not in message
    ]


def route_share(client: Any, messages: list[dict]) -> Callable[[list[dict]], float] | None:
    """The client's own measure of the share of its route's admission bound that the standing
    request in ``messages`` takes with a tool list; None when the client has no such measure."""
    measure = getattr(client, "request_capacity_ratio", None)
    if not callable(measure):
        return None
    standing = standing_messages(messages)
    return lambda tools: measure(messages=standing, tools=tools or None)


class ToolRoom:
    """How one turn's tools are offered, step by step.

    Each different list (with the standing request it is measured with) is measured once, and the
    turn keeps the most compact mode it has used.
    """

    def __init__(self, context_window: int):
        self.context_window = context_window
        self.mode = FULL
        self._last: tuple[str, Offer] | None = None
        self._recorded: tuple[str, tuple[str, ...]] | None = None

    def offer(
        self,
        schemas: list[dict],
        *,
        apps: Mapping[str, str] | None = None,
        client: Any = None,
        messages: list[dict] | None = None,
    ) -> tuple[Offer, dict | None]:
        """This step's offer, and the turn's ``tools_offered`` event data when its mode or
        reasons are new in this turn (None otherwise)."""
        if not schemas:
            return Offer([]), None
        share = route_share(client, messages or [])
        measured = encode(
            [
                schemas,
                dict(apps or {}),
                standing_messages(messages or []) if share is not None else None,
            ]
        )
        if self._last is not None and self._last[0] == measured + self.mode:
            return self._last[1], None
        made = offer(
            schemas,
            context_window=self.context_window,
            apps=apps,
            route_share=share,
            at_least=self.mode,
        )
        if MODES.index(made.mode) > MODES.index(self.mode):
            self.mode = made.mode
        # The same list measured again from the mode it moved to is offered the same way.
        self._last = (measured + self.mode, made)
        if (made.mode, made.reasons) == self._recorded:
            return made, None
        self._recorded = (made.mode, made.reasons)
        measures = made.measures or _measure(schemas, None)
        return made, {
            "mode": made.mode,
            "reasons": list(made.reasons),
            "tools": measures.tools,
            "direct": len(made.schemas),
            "by_lookup": len(made.hidden),
            "schema_tokens": measures.schema_tokens,
            "context_window": self.context_window,
            "route_share": round(measures.route_share, 3),
        }


def _summary(description: str) -> str:
    """The first sentence of a tool's description, kept short."""
    first = " ".join(description.split()).split(". ", 1)[0].rstrip(".")
    return first if len(first) <= 100 else first[:99].rstrip() + "…"


def _compact_schemas(hidden: dict[str, dict], lines: Mapping[str, str]) -> list[dict]:
    names = list(hidden)
    listed = "; ".join(
        f"{name}: {lines.get(name) or _summary(schema['function'].get('description') or '')}"
        for name, schema in hidden.items()
    )
    return [
        {
            "type": "function",
            "function": {
                "name": LOOKUP_TOOL,
                "description": "More tools can be used than are listed directly. Get one's full "
                f"description and inputs by name, then run it with {CALL_THROUGH_TOOL}. They are: "
                + listed
                + ".",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "enum": names}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": CALL_THROUGH_TOOL,
                "description": f"Run a tool listed in {LOOKUP_TOOL} with the arguments its inputs "
                "describe. It runs exactly as a direct call would, with the same checks and "
                "approvals, and returns that tool's result.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": names},
                        "arguments": {"type": "object"},
                    },
                    "required": ["name", "arguments"],
                    "additionalProperties": False,
                },
            },
        },
    ]
