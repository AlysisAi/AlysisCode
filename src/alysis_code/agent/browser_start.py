"""Opt-in browser handoff tool; this channel never reads or operates a page."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..computer_use_attribution import scope_refusal
from ..computer_use_platform_policy import (
    mac_w7_computer_use_available,
    refuse_computer_use,
    refuse_unavailable_mac_w7_computer_use,
)
from ..skills.computer_use import ordinary_entry_guidance
from .tool_definition import ToolDef


@dataclass(frozen=True)
class BrowserStartHandoff:
    """Process-only control result, never accepted from model JSON or tool hooks."""

    request_id: str


class ComputerSelectionCorrection(dict[str, Any]):
    """Local-only evidence that no desktop handoff was requested; host JSON cannot mint it."""


class ComputerDiscoveryTerminal(dict[str, Any]):
    """Local wrapper evidence that this captured inventory cannot continue safely."""

    def __init__(self, value: dict[str, Any]) -> None:
        super().__init__(value)
        self.reason = str(value["error"])


COMPUTER_DISCOVERY_TERMINAL_MESSAGES = {
    "computer_discovery_unavailable": (
        "Alysis could not confirm an app list for this request. No Computer Use session was started. "
        "Check Any App setup if needed, then send your request again."
    ),
    "computer_discovery_stale": (
        "Alysis could not confirm the selected app using this request's app list. "
        "No Computer Use session was started. Please send your request again to use a fresh app list."
    ),
    "computer_use_target_stale": (
        "Alysis could not confirm that the selected app still matches this request's app list. "
        "No Computer Use session was started. Please send your request again to use a fresh app list."
    ),
    "computer_use_inventory_unavailable": (
        "Alysis could not finish checking the selected app. No Computer Use session was started. "
        "Please send your request again to use a fresh app list."
    ),
}


class _DiscoveredComputerStartTool(ToolDef):
    """Keep exact controller validation while delivering app labels on demand."""

    def as_openai_tool(self) -> dict[str, Any]:
        import copy

        tool = super().as_openai_tool()
        # ToolDef's ordinary projection may share its parameter object. Never
        # remove the controller's enum or mutate another request's schema.
        parameters = copy.deepcopy(tool["function"]["parameters"])
        parameters["properties"]["app_steps"]["items"]["properties"]["app"].pop("enum", None)
        tool["function"]["parameters"] = parameters
        return tool


def valid_browser_start_url(value: Any) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return False
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value:
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.port != 0
        )
    except ValueError:
        return False


def build_browser_start_tool(request: Callable[[str], Any]) -> ToolDef:
    refuse_computer_use()

    def run(arguments: dict[str, Any]) -> Any:
        refuse_computer_use()
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"url"}
            or not valid_browser_start_url(arguments.get("url"))
        ):
            return {
                "error": "browser_start_invalid_url",
                "message": "Supply one HTTPS URL without credentials, at most 2048 characters.",
            }
        result = request(arguments["url"])
        refuse_computer_use()
        return result

    return ToolDef(
        name="browser_start",
        description=(
            "Start a browser task from this chat, including on New Tab. Use this when the user wants "
            "to open or work on a website and browser tools are not yet present. Supply the appropriate "
            "HTTPS starting URL. The host continues the SAME original user request with browser tools; "
            "do not ask the user to repeat it. This tool only prepares that handoff; it does not itself "
            "prove navigation or task completion. Do not use it for greetings or research that needs no browser interaction."
        ),
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "maxLength": 2048}},
            "required": ["url"],
            "additionalProperties": False,
        },
        run=run,
        # Supplied by the desktop host for this session only. The name is shared with
        # the managed-browser tool, so the process-wide optional-tool registry must not
        # decide its availability.
        metadata={"host_owned": True},
    )


COMPUTER_ACTIONS = (
    "press",
    "focus",
    "click",
    "type",
    "set_value",
    "key",
    "scroll",
    "drag",
    "history_back",
    "activate_application",
    "quit_application",
)

# Legacy native background delivery only. New synthetic/background input routes
# are not enabled by the legacy Mac availability predicate.
NATIVE_UNSUPPORTED_ACTIONS = frozenset({"type", "key", "scroll", "drag", "history_back"})


def native_unsupported_start_actions() -> frozenset[str]:
    """Partition plan admission using the live host, not model or cached flags.

    Mac W7 plans may declare focused key/type and visual scroll/drag. Their
    execution still requires the original negotiated interaction-v2 validators.
    Browser history navigation never becomes a native action.
    """
    if mac_w7_computer_use_available():
        return frozenset({"history_back"})
    return NATIVE_UNSUPPORTED_ACTIONS


COMPUTER_SELECTION_CORRECTION = "correct_selection_not_dispatched"
# Everything except opening or switching apps changes something inside an app.
COMPUTER_INPUT_ACTIONS = frozenset(COMPUTER_ACTIONS) - {"activate_application"}
# The model declares only its plan. The host still receives exactly these four selection fields,
# derived from that plan: no second copy of identifiers or actions for the model to keep in sync.
COMPUTER_START_KEYS = frozenset({"goal", "constraints", "operates_apps", "app_steps"})
COMPUTER_HOST_SELECTION_KEYS = ("target_ids", "allowed_actions", "goal", "constraints")
MAX_COMPUTER_PLAN_STEPS = 16
MAX_COMPUTER_PLAN_APPS = 8
# Entries in one step's actions, repeats included (repeats name the same kind of action once).
MAX_STEP_ACTION_ENTRIES = len(COMPUTER_ACTIONS)
# An app that is not running, or is running with no open window, is opened by the step that uses it.
COMPUTER_OPEN_REQUIRED = frozenset({"installed", "no_window"})


def computer_app_labels(names: list[str]) -> list[str]:
    """One exact label per captured target, in inventory order.

    A label is the app's listed name. Repeated names are numbered on every copy, so no copy
    silently owns the plain name; a label never resolves to more than one target.
    """
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    used = {name for name in names if counts[name] == 1}
    numbered: dict[str, int] = {}
    labels: list[str] = []
    for name in names:
        if counts[name] == 1:
            labels.append(name)
            continue
        while True:
            numbered[name] = numbered.get(name, 0) + 1
            label = f"{name} ({numbered[name]})"
            if label not in used:
                break
        used.add(label)
        labels.append(label)
    return labels


def computer_start_problem(value: Any, labels: Any) -> str | None:
    """A fixed reason the call does not follow the computer_start contract, or None.

    actions names the kinds of action a step needs. Models often write it as a sequence, so a kind
    named more than once is the same declaration: it is accepted and never adds authority.
    """
    if not isinstance(value, dict):
        return "Send one object with goal, constraints, operates_apps and app_steps."
    if {"target_ids", "allowed_actions"} & set(value):
        return "Do not send target_ids or allowed_actions: the selected apps and actions are derived from app_steps."
    if set(value) != COMPUTER_START_KEYS:
        return "Send exactly goal, constraints, operates_apps and app_steps, with no other fields."
    goal, constraints = value["goal"], value["constraints"]
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 2000:
        return "goal must be a non-empty string of at most 2000 characters."
    if (
        not isinstance(constraints, list)
        or len(constraints) > 16
        or not all(isinstance(c, str) and 1 <= len(c) <= 1000 for c in constraints)
    ):
        return "constraints must be a list of at most 16 non-empty strings of at most 1000 characters each."
    if type(value["operates_apps"]) is not bool:
        return "operates_apps must be true or false."
    steps = value["app_steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_COMPUTER_PLAN_STEPS:
        return f"app_steps must list 1 to {MAX_COMPUTER_PLAN_STEPS} steps."
    for number, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or set(step) != {"app", "actions"}:
            return f"Step {number} must have exactly app and actions."
        if not isinstance(step["app"], str) or step["app"] not in labels:
            return f"Step {number}'s app is not one of the listed app labels; copy it exactly from available_targets."
        actions = step["actions"]
        if not isinstance(actions, list) or len(actions) > MAX_STEP_ACTION_ENTRIES:
            return f"Step {number}'s actions must be a list of at most {MAX_STEP_ACTION_ENTRIES} action names."
        if not all(isinstance(action, str) and action in COMPUTER_ACTIONS for action in actions):
            return (
                f"Step {number} names an unknown action; use only: {', '.join(COMPUTER_ACTIONS)}."
            )
    if len({step["app"] for step in steps}) > MAX_COMPUTER_PLAN_APPS:
        return f"Use at most {MAX_COMPUTER_PLAN_APPS} different apps."
    return None


def valid_computer_start(value: Any, labels: Any) -> bool:
    return computer_start_problem(value, labels) is None


def computer_plan_problem(value: dict[str, Any], targets: dict[str, dict[str, Any]]) -> str | None:
    """Fixed explanation when the plan cannot be carried out by the scope it implies.

    The protected run can never gain an app or an action after the handoff, so a plan whose
    first action would be refused fails before anything starts. This only refuses; it never
    adds an app or an action, and it reads no request prose.
    """
    steps = value["app_steps"]
    planned = {action for step in steps for action in step["actions"]}
    if value["operates_apps"] and not planned & COMPUTER_INPUT_ACTIONS:
        scope_refusal("operates_true_without_input")
        return (
            "operates_apps is true, but no step lists an input action, so the first press or edit would be "
            "refused. For each app, list the actions its step needs: press for buttons and menu items, "
            "set_value for text fields, focus, click; other native input requires its currently advertised "
            "background capability. Missing capability must not be bypassed through foreground mode. "
            "Keep every prohibition from the original request."
        )
    if not value["operates_apps"] and planned & COMPUTER_INPUT_ACTIONS:
        scope_refusal("operates_false_with_input")
        return (
            "operates_apps is false, but app_steps list input actions. A request that only opens or reads "
            "apps uses [] or activate_application; set operates_apps to true only if the request asks to "
            "press, enter, edit, save or otherwise change something."
        )
    for index, step in enumerate(steps):
        if (
            index
            and step["app"] != steps[index - 1]["app"]
            and "activate_application" not in step["actions"]
        ):
            scope_refusal("app_switch_without_activation")
            return (
                "A step that moves to a different app than the previous step must include activate_application; "
                "the host refuses switching apps without it."
            )
        if (
            not index
            and targets[step["app"]].get("availability") in COMPUTER_OPEN_REQUIRED
            and "activate_application" not in step["actions"]
        ):
            scope_refusal("initial_open_without_activation")
            return (
                "The first app is marked installed or no_window, so its step must include activate_application "
                "to launch it or ask the running app to reopen a window."
            )
    return None


def valid_computer_selection(value: Any, discovery: Any) -> bool:
    """The four host fields, checked again at the bridge against the host-issued inventory."""
    return (
        isinstance(value, dict)
        and set(value) == set(COMPUTER_HOST_SELECTION_KEYS)
        and isinstance(discovery, dict)
        and type(discovery.get("version")) is int
        and discovery["version"] == 1
        and isinstance(discovery.get("targets"), list)
        and isinstance(value["target_ids"], list)
        and 1 <= len(value["target_ids"]) <= MAX_COMPUTER_PLAN_APPS
        and all(
            isinstance(i, str)
            and any(isinstance(t, dict) and t.get("target_id") == i for t in discovery["targets"])
            for i in value["target_ids"]
        )
        and len(set(value["target_ids"])) == len(value["target_ids"])
        and isinstance(value["allowed_actions"], list)
        and len(value["allowed_actions"]) <= len(COMPUTER_ACTIONS)
        and all(isinstance(a, str) and a in COMPUTER_ACTIONS for a in value["allowed_actions"])
        and len(set(value["allowed_actions"])) == len(value["allowed_actions"])
        and isinstance(value["goal"], str)
        and bool(value["goal"].strip())
        and len(value["goal"]) <= 2000
        and isinstance(value["constraints"], list)
        and len(value["constraints"]) <= 16
        and all(isinstance(c, str) and 1 <= len(c) <= 1000 for c in value["constraints"])
    )


# The desktop host's closed admission refusals for a selection. Codes are host-issued; the
# explanations are the engine's own fixed text and never repeat host or app content.
_INVENTORY_REFRESH_REQUIRED = (
    "This request cannot refresh its app list: it was captured once, when the request started. "
    "Tell the user what happened and ask them to send the request again, which captures a fresh "
    "app list. Do not retry these apps or claim that apps were rediscovered."
)
COMPUTER_TARGET_UNVERIFIED = "computer_use_target_unverified"
COMPUTER_START_REFUSALS: dict[str, str] = {
    COMPUTER_TARGET_UNVERIFIED: (
        "The host could not verify a selected app's installation for this request, so nothing was started. "
        "Do not retry that app in this request. Do not claim it changed, quit, is missing, or will succeed "
        "merely by resending."
    ),
    "computer_use_target_stale": (
        "A selected app changed after this request's app list was captured (it quit, restarted "
        "or was replaced), so nothing was started. " + _INVENTORY_REFRESH_REQUIRED
    ),
    "computer_use_inventory_unavailable": (
        "The host could not re-check the selected app right now (its app scan did not finish), "
        "so nothing was started. Do not say the app is missing or not installed. "
        + _INVENTORY_REFRESH_REQUIRED
    ),
    "computer_use_activation_required": (
        "A selected app is not running or has no open window, so nothing was started. Opening it "
        "requires activate_application in the step that uses it; include it only when the user's "
        "request allows opening the app."
    ),
    "computer_use_chrome_scope_required": (
        "A Chrome tab target must be selected on its own, without other targets. Nothing was "
        "started."
    ),
    "computer_use_start_request_invalid": (
        "The host did not accept this selection, so nothing was started. Use app labels exactly "
        "as listed by computer_discover in this request."
    ),
}
# Refusals after which the captured inventory cannot be trusted for the rest of the request.
_FRESH_TURN_REFUSALS = frozenset(
    {"computer_use_target_stale", "computer_use_inventory_unavailable"}
)
_SELECTION_CONTRACT = (
    "computer_start takes goal (non-empty, up to 2000 characters), constraints (at most 16), operates_apps "
    "and app_steps: 1-16 steps in the order the request uses the apps, at most 8 different apps, each step "
    "with app copied exactly from available_targets and actions naming each kind of action that step needs "
    "(a set, not a sequence: repeats and order do not matter). Under normal approval the selected apps and "
    "actions are exactly those in app_steps. For native apps, use semantic press, focus, set_value or click, and "
    "activate_application when opening or switching to an app. Selecting the initial already-open app "
    "for background observation does not itself require activation. List every action kind the request needs; "
    "choose its input route later from the current protected observation. Native v2 background event input "
    "requires that action's advertised capability. Foreground mode requires a separate host-owned human "
    "handoff; no such handoff is available in the background session, and model arguments or Full Access "
    "cannot grant it. Application transitions do not authorize activating or raising the target. "
    "history_back is browser-only."
)


def valid_unverified_target_refusal(
    value: Any, selected_ids: tuple[str, ...], inventory_ids: frozenset[str]
) -> bool:
    """Accept only the closed host refusal correlated to this exact captured selection."""
    if (
        not isinstance(value, dict)
        or set(value) != {"error", "unverified_target_ids"}
        or value.get("error") != COMPUTER_TARGET_UNVERIFIED
    ):
        return False
    ids = value["unverified_target_ids"]
    return (
        isinstance(ids, list)
        and 1 <= len(ids) <= MAX_COMPUTER_PLAN_APPS
        and all(isinstance(target_id, str) for target_id in ids)
        and len(set(ids)) == len(ids)
        and set(ids) <= set(selected_ids) & inventory_ids
    )


def build_computer_start_tools(
    request: Callable[[Any], Any], discovery: dict[str, Any]
) -> dict[str, ToolDef]:
    refuse_computer_use()
    import copy

    if "application_lifecycle_version" in discovery:
        refuse_unavailable_mac_w7_computer_use()
    discovery = copy.deepcopy(discovery)
    targets = [
        target
        for target in discovery.get("targets", [])
        if isinstance(target, dict)
        and isinstance(target.get("target_id"), str)
        and isinstance(target.get("name"), str)
        and target["name"]
    ]
    labels = computer_app_labels([target["name"] for target in targets])
    by_label = dict(zip(labels, targets, strict=True))
    # The host-issued reference is identity only. Selection still accepts exact
    # labels and maps them back to this pinned inventory; it accepts no model IDs.
    apps = [
        {
            "app": label,
            "application_ref": target["target_id"],
            "route": target.get("route"),
            **(
                {"availability": target["availability"]}
                if target.get("availability") in COMPUTER_OPEN_REQUIRED
                else {}
            ),
        }
        for label, target in by_label.items()
    ]
    lifecycle_available = (
        type(discovery.get("application_lifecycle_version")) is int
        and discovery["application_lifecycle_version"] == 1
    )
    scope_actions = [
        action for action in COMPUTER_ACTIONS if action != "quit_application" or lifecycle_available
    ]
    if targets and all(target.get("route") == "native" for target in targets):
        scope_actions = [
            action for action in scope_actions if action not in native_unsupported_start_actions()
        ]
    # Set once the host refuses a selection because the captured inventory no longer holds.
    # The inventory is never refreshed inside a request, so later calls say so instead of
    # returning the same stale identities.
    stale: dict[str, str] = {}
    # Private host evidence, keyed by identity rather than a plan or its ordering.
    unverified_ids: set[str] = set()
    inventory_ids = frozenset(target["target_id"] for target in targets)
    invalid_verification = False

    def invalid_verification_refusal() -> dict[str, Any]:
        return {
            "error": "computer_use_start_request_invalid",
            "message": (
                "The host's app-verification response could not be validated. "
                "No Computer Use session was started. Further app selection is blocked for this request."
            ),
        }

    def correction(code: str, message: str) -> dict[str, Any]:
        return ComputerSelectionCorrection(
            {
                "error": code,
                "message": message,
                "recovery": COMPUTER_SELECTION_CORRECTION,
                # Re-offer only the same captured inventory. Never guess an app from a
                # misspelled label or silently add actions to the requested authority.
                "available_targets": copy.deepcopy(apps),
            }
        )

    def refusal(code: str) -> dict[str, Any]:
        result = {
            "error": code,
            "message": COMPUTER_START_REFUSALS[code],
            **({"fresh_turn_required": True} if code in _FRESH_TURN_REFUSALS else {}),
        }
        return ComputerDiscoveryTerminal(result) if code in _FRESH_TURN_REFUSALS else result

    def unavailable() -> dict[str, Any]:
        return ComputerDiscoveryTerminal(
            {
                "error": "computer_discovery_unavailable",
                "fresh_turn_required": True,
                "message": (
                    "No selectable apps were captured for this request. This does not prove that an app "
                    "is missing or that macOS permission was denied. Ask the user to check "
                    "Settings > Computer use > Any App and complete setup if needed, then send a new request. "
                    "This request's app list cannot refresh. Do not retry discovery or selection, guess app "
                    "identifiers, or use another computer-control route to work around unavailable discovery. "
                    "Do not claim an app was opened or changed."
                ),
            }
        )

    def discover(arguments: dict[str, Any]) -> Any:
        refuse_computer_use()
        if arguments:
            return {"error": "invalid_arguments"}
        if not targets:
            return unavailable()
        if stale:
            return ComputerDiscoveryTerminal(
                {
                    "error": "computer_discovery_stale",
                    "fresh_turn_required": True,
                    "message": _INVENTORY_REFRESH_REQUIRED,
                }
            )
        return {"version": 1, "targets": copy.deepcopy(apps)}

    def start(arguments: dict[str, Any]) -> Any:
        refuse_computer_use()
        nonlocal invalid_verification
        if invalid_verification:
            return invalid_verification_refusal()
        # No choice can repair an empty captured inventory. In particular, do not
        # trigger a forced-selection recovery request with no available targets.
        if not targets:
            return unavailable()
        if stale:
            return refusal(stale["code"])
        invalid = computer_start_problem(arguments, by_label)
        if invalid:
            return correction(
                "computer_start_invalid_selection",
                f"Nothing was started. {invalid} {_SELECTION_CONTRACT}",
            )
        # A kind of action named more than once in a step is one declaration.
        steps = [
            {"app": step["app"], "actions": list(dict.fromkeys(step["actions"]))}
            for step in arguments["app_steps"]
        ]
        arguments = {**arguments, "app_steps": steps}
        selected = [by_label[label] for label in dict.fromkeys(step["app"] for step in steps)]
        if any(target["target_id"] in unverified_ids for target in selected):
            return refusal(COMPUTER_TARGET_UNVERIFIED)
        # Exactly the planned actions, in canonical order. Nothing outside app_steps is granted.
        planned = [
            action
            for action in COMPUTER_ACTIONS
            if any(action in step["actions"] for step in steps)
        ]
        if any(
            target.get("route") == "native" for target in selected
        ) and native_unsupported_start_actions().intersection(planned):
            return correction(
                "computer_start_unsupported_native_actions",
                "Nothing was started. These planned actions are unavailable for native control on this host: "
                + ", ".join(
                    action
                    for action in COMPUTER_ACTIONS
                    if action in native_unsupported_start_actions()
                )
                + ". history_back is browser-only. Foreground input remains unavailable. "
                "Correct the step actions using only the actions needed for "
                "the original user request, preserving every constraint. Use [] for observation only.",
            )
        if "quit_application" in planned and (
            not lifecycle_available or any(t.get("route") != "native" for t in selected)
        ):
            return correction(
                "computer_start_unsupported_native_actions",
                "Normal application quit is not supported by this selected host. Nothing was started.",
            )
        problem = computer_plan_problem(arguments, by_label)
        if problem:
            return correction(
                "computer_start_scope_inconsistent", "Nothing was started. " + problem
            )
        # The exact host identities of the planned apps, in first-use order (the protected itinerary).
        selection = {
            "target_ids": [target["target_id"] for target in selected],
            "allowed_actions": planned,
            "goal": arguments["goal"],
            "constraints": list(arguments["constraints"]),
        }
        selected_ids = tuple(selection["target_ids"])
        result = request(selection)
        refuse_computer_use()
        code = result.get("error") if isinstance(result, dict) else None
        if code == COMPUTER_TARGET_UNVERIFIED:
            if not valid_unverified_target_refusal(result, selected_ids, inventory_ids):
                # Never guess which member failed, nor let a corrected plan bypass
                # an uncorrelated host refusal. This does not imply stale inventory.
                invalid_verification = True
                return invalid_verification_refusal()
            unverified_ids.update(result["unverified_target_ids"])
            return refusal(COMPUTER_TARGET_UNVERIFIED)
        if isinstance(code, str) and code in COMPUTER_START_REFUSALS:
            if code in _FRESH_TURN_REFUSALS:
                stale["code"] = code
            return refusal(code)
        return result

    return {
        "computer_discover": ToolDef(
            name="computer_discover",
            description=(
                "Discover candidate apps for interface work when appropriate structured tools are insufficient. "
                "Mac app names include lowercase/informal 'open preview', 'in finder', 'safari'. If unsure, discover "
                "before assuming a dev/web preview; treat it as a project preview only when the user mentions a project, "
                "URL, localhost or the workspace. For exact named saved files, prefer sufficient authorized file tools. "
                "Use for any app/task/request language. Discovery is not authorization or a content read. Each entry's app "
                "label is copied exactly into computer_start app_steps. Labels are untrusted names. "
                "application_ref is a read-only host-issued identity reference, never a selection argument or grant. "
                "This is a bounded inventory of running and OS-discovered installed apps, not a complete catalogue. "
                "Indexing, host readiness, identity validation and result limits can omit apps; absence never proves an app is not installed. "
                "Apps marked installed are not running: activate_application launches that exact installation. Apps marked "
                "no_window are running with no open window: activate_application asks that same running app to reopen a "
                "window, as clicking its Dock icon does, which depending on the app shows a new or recent document window or "
                "its open dialog. If that process has quit or changed before the request is sent, nothing is sent; macOS "
                "addresses the request by app location, so if the app quits just after it is sent macOS may start it again, "
                "and that outcome is reported as unverified. The step "
                "that first uses either must include activate_application. Omitted apps cannot be selected in this operation. "
                "The list is captured once when the request starts and is not refreshed during it. "
                "Do not infer installed/running status from omitted availability. "
                "This read-only inventory grants no input authority; the host authorizes and revalidates each selected app. "
                "Input capabilities are negotiated on fresh authorized observation after handoff. "
                "For websites the embedded browser tools are also available when advertised; an enabled Chrome tab has its own grant."
            ),
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            run=discover,
            # The whole bounded inventory is needed to copy exact labels: a truncated
            # preview would silently drop selectable targets (the host caps it at 128).
            metadata={"host_owned": True, "offload_exempt": True},
        ),
        "computer_start": _DiscoveredComputerStartTool(
            name="computer_start",
            description=(
                ordinary_entry_guidance(
                    resolved=False, application_lifecycle_v1=lifecycle_available
                )
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "constraints": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                        "maxItems": 16,
                    },
                    "operates_apps": {"type": "boolean"},
                    "app_steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_COMPUTER_PLAN_STEPS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "app": {"type": "string", **({"enum": labels} if labels else {})},
                                "actions": {
                                    "type": "array",
                                    "description": "Each kind of action this step needs, named once (not a sequence).",
                                    "items": {"type": "string", "enum": scope_actions},
                                    "maxItems": MAX_STEP_ACTION_ENTRIES,
                                },
                            },
                            "required": ["app", "actions"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["goal", "constraints", "operates_apps", "app_steps"],
                "additionalProperties": False,
            },
            run=start,
            # Correctable selection errors carry the bounded inventory too.
            metadata={"host_owned": True, "offload_exempt": True},
        ),
    }
