"""computer_start scope must be able to carry out its own plan before anything reaches the desktop.

Reproduces the scope shape observed in operation 709e57c6 (12 Oct 2026): the stage log and
session log record one selected target, allowed_actions=["activate_application"] and one
constraint, followed by a press refused out_of_scope. The host guard was correct; the scope
admitted before the handoff was not. The goal and constraint strings below are fixture values,
not the captured payload. The model now declares only app_steps by exact app label; the host's
four selection fields are derived from that plan. No real apps, providers or desktop input are used.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from test_computer_discovery_recovery import _selection_session

from alysis_code.agent.browser_start import (
    COMPUTER_ACTIONS,
    BrowserStartHandoff,
    build_computer_start_tools,
)
from alysis_code.llm.openai_compat import LLMResponse, ToolCall

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

REQUEST = (
    "Open Calculator and calculate 12345 × 6789. Write the calculation and result in a new TextEdit "
    "document. Save it as “Calculation.txt” inside a new Desktop folder named “Alysis Test October 12”. "
    "Close and reopen the document, then confirm what was saved."
)


def test_quit_planning_requires_negotiated_native_inventory():
    for version in [None, 1]:
        inventory = _apps()
        if version is not None:
            inventory["application_lifecycle_version"] = version
        sent = []
        tools = build_computer_start_tools(
            lambda request, sent=sent: sent.append(request) or BrowserStartHandoff("bound"),
            inventory,
        )
        tool = tools["computer_start"]
        actions = tool.parameters["properties"]["app_steps"]["items"]["properties"]["actions"][
            "items"
        ]["enum"]
        assert ("quit_application" in actions) == (version == 1)
        result = tool.run(_plan([("Calculator", ["quit_application"])]))
        assert bool(sent) == (version == 1)
        if version is None:
            assert "error" in result


def _apps() -> dict[str, Any]:
    names = [
        ("Calculator", None),
        ("TextEdit", None),
        ("Finder", None),
        ("Σημειώσεις", "installed"),
    ]
    return {
        "version": 1,
        "targets": [
            {
                "target_id": str(uuid.UUID(int=index + 1, version=4)),
                "name": name,
                "route": "native",
                **({"availability": availability} if availability else {}),
            }
            for index, (name, availability) in enumerate(names)
        ],
    }


def _ids(inventory: dict[str, Any]) -> dict[str, str]:
    return {target["name"]: target["target_id"] for target in inventory["targets"]}


def _start(arguments: dict[str, Any], call_id: str = "start") -> LLMResponse:
    return LLMResponse(
        content="",
        raw={},
        tool_calls=[ToolCall(id=call_id, name="computer_start", arguments=arguments)],
    )


def _discover() -> LLMResponse:
    return LLMResponse(
        content="",
        raw={},
        tool_calls=[ToolCall(id="discover", name="computer_discover", arguments={})],
    )


def _run(tmp_path, inventory, responses):
    dispatched: list[dict[str, Any]] = []
    session, client = _selection_session(
        tmp_path,
        inventory,
        responses,
        lambda args: dispatched.append(args) or BrowserStartHandoff("bound"),
    )
    try:
        exit_code = session.run_turn(REQUEST)
    finally:
        session.close()
    return exit_code, dispatched, client


def test_the_observed_activation_only_scope_never_reaches_the_desktop(tmp_path):
    inventory = _apps()
    calculator = _ids(inventory)["Calculator"]
    # The observed shape: one app, activation only, one constraint (fixture text), for a request
    # that operates apps.
    observed = {
        "target_ids": [calculator],
        "allowed_actions": ["activate_application"],
        "goal": REQUEST,
        "constraints": ["Fixture constraint."],
    }
    responses = [
        _discover(),
        _start(observed),
        _discover(),
        _start(observed, "start-2"),
        _discover(),
        _start(observed, "start-3"),
    ]
    exit_code, dispatched, client = _run(tmp_path, inventory, responses)
    assert dispatched == [], (
        "an admitted scope with no input action could only fail at the first press"
    )
    assert exit_code == 1
    # Legacy four-field input is a bounded, local correction, never an admission.
    results = [
        json.loads(m["content"])
        for m in client.call_records[-1]["messages"]
        if m.get("role") == "tool" and "computer_start" in m.get("content", "")
    ]
    assert {result["error"] for result in results} == {"computer_start_invalid_selection"}
    assert all(result["recovery"] == "correct_selection_not_dispatched" for result in results)
    assert client.calls == len(responses), "no provider call beyond the existing retry limit"


def _plan(
    steps: list[tuple[str, list[str]]],
    *,
    operates: bool = True,
    constraints: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "goal": REQUEST,
        "constraints": constraints or ["Fixture constraint."],
        "operates_apps": operates,
        "app_steps": [{"app": name, "actions": actions} for name, actions in steps],
    }


def _host(ids: dict[str, str], plan: dict[str, Any]) -> dict[str, Any]:
    steps = plan["app_steps"]
    return {
        "target_ids": [ids[app] for app in dict.fromkeys(step["app"] for step in steps)],
        "allowed_actions": [
            a for a in COMPUTER_ACTIONS if any(a in step["actions"] for step in steps)
        ],
        "goal": plan["goal"],
        "constraints": plan["constraints"],
    }


def test_background_click_then_type_plan_keeps_exact_goal_constraints_and_scope():
    inventory = {
        "version": 1,
        "targets": [
            {
                "target_id": str(uuid.UUID(int=71, version=4)),
                "name": "Fixture Editor",
                "route": "native",
            }
        ],
    }
    sent = []
    tools = build_computer_start_tools(
        lambda selection: sent.append(selection) or BrowserStartHandoff("bound"), inventory
    )
    discovered = tools["computer_discover"].run({})
    label = discovered["targets"][0]["app"]
    goal = "In the background, enter the requested text in the empty custom editor."
    constraints = ["Do not activate the app or use foreground control."]
    plan = {
        "goal": goal,
        "constraints": constraints,
        "operates_apps": True,
        "app_steps": [{"app": label, "actions": ["click", "type"]}],
    }
    assert isinstance(tools["computer_start"].run(plan), BrowserStartHandoff)
    assert sent == [
        {
            "target_ids": [inventory["targets"][0]["target_id"]],
            "allowed_actions": ["click", "type"],
            "goal": goal,
            "constraints": constraints,
        }
    ]
    # Both the runtime tool description and a real local correction must describe
    # conditional route choice, rather than contradicting this admitted plan.
    correction = tools["computer_start"].run({**plan, "app_steps": []})
    contract = tools["computer_start"].description + correction["message"]
    assert "current" in contract and "background_actions" in contract
    assert "later action need not be available" in contract.lower()
    assert (
        "initial already-open app" in contract and "does not itself require activation" in contract
    )
    assert "each later action must explicitly select input_mode foreground" not in contract
    assert "Native type, key, scroll and drag require explicit foreground" not in contract
    assert "separate host-owned human handoff" in tools["computer_start"].description
    assert "none is available in this background session" in tools["computer_start"].description
    assert (
        "Foreground control requires explicit input_mode foreground"
        not in tools["computer_start"].description
    )
    assert len(sent) == 1, "the invalid selection cannot widen or repeat the handoff"


CROSS_APP = [
    ("Calculator", ["press"]),
    ("TextEdit", ["activate_application", "set_value", "press"]),
]


def test_the_observed_failure_is_corrected_into_one_cross_app_handoff_with_only_the_planned_scope(
    tmp_path,
):
    inventory = _apps()
    ids = _ids(inventory)
    activation_only = _plan([("Calculator", ["activate_application"])])
    corrected = _plan(CROSS_APP)
    exit_code, dispatched, client = _run(
        tmp_path, inventory, [_discover(), _start(activation_only), _start(corrected, "start-2")]
    )
    assert exit_code == 0
    assert dispatched == [_host(ids, corrected)], (
        "the host receives exactly the planned apps and actions"
    )
    assert dispatched[0]["target_ids"] == [ids["Calculator"], ids["TextEdit"]], (
        "itinerary keeps the plan's order"
    )
    correction = [m for m in client.call_records[2]["messages"] if m.get("role") == "tool"][-1][
        "content"
    ]
    assert (
        "computer_start_scope_inconsistent" in correction
        and "no step lists an input action" in correction
    )
    assert client.call_records[2]["tool_choice"]["function"]["name"] == "computer_start"


def test_inconsistent_plans_are_refused_before_handoff_and_never_widened():
    from alysis_code.agent.browser_start import build_computer_start_tools

    inventory = _apps()
    ids = _ids(inventory)
    calls: list[dict[str, Any]] = []
    start = build_computer_start_tools(
        lambda args: calls.append(args) or BrowserStartHandoff("bound"), inventory
    )["computer_start"]
    good = _plan(CROSS_APP)
    cases = {
        "switch without activation": _plan(
            [("Calculator", ["press"]), ("TextEdit", ["set_value", "press"])]
        ),
        "operates without input": _plan([("Calculator", ["activate_application"])]),
        "reads but lists input": _plan([("Calculator", ["press"])], operates=False),
        "installed app not opened": _plan([("Σημειώσεις", ["press"])]),
    }
    for label, selection in cases.items():
        result = start.run(selection)
        assert result.get("error") == "computer_start_scope_inconsistent", label
        assert result["recovery"] == "correct_selection_not_dispatched", label
    malformed = {
        "no plan": {k: v for k, v in good.items() if k not in ("operates_apps", "app_steps")},
        "unlisted app": {**good, "app_steps": [{"app": "Text Edit", "actions": ["press"]}]},
        "host ID instead of label": {
            **good,
            "app_steps": [{"app": ids["Calculator"], "actions": ["press"]}],
        },
        "duplicate target declaration": {
            **good,
            "target_ids": [ids["Calculator"], ids["TextEdit"]],
        },
        "duplicate action declaration": {
            **good,
            "allowed_actions": ["press", "set_value", "activate_application"],
        },
        "extra field": {**good, "note": "x"},
    }
    for label, selection in malformed.items():
        assert start.run(selection).get("error") == "computer_start_invalid_selection", label
    nine = {
        "version": 1,
        "targets": [
            {
                "target_id": str(uuid.UUID(int=100 + i, version=4)),
                "name": f"App {i}",
                "route": "native",
            }
            for i in range(9)
        ],
    }
    wide = build_computer_start_tools(lambda args: calls.append(args), nine)["computer_start"]
    assert (
        wide.run(_plan([(f"App {i}", ["activate_application"]) for i in range(9)], operates=False))[
            "error"
        ]
        == "computer_start_invalid_selection"
    ), "at most 8 different apps, as the host admits"
    assert calls == [], "no inconsistent or malformed scope reaches the desktop"
    # Observation only and a returning app remain valid plans.
    read_only = _plan([("Calculator", [])], operates=False)
    returning = _plan(
        [
            ("Calculator", ["press"]),
            ("TextEdit", ["activate_application", "set_value"]),
            ("Calculator", ["activate_application", "press"]),
        ]
    )
    for selection in (read_only, good, returning):
        assert isinstance(start.run(selection), BrowserStartHandoff)
    assert calls == [_host(ids, read_only), _host(ids, good), _host(ids, returning)]
    assert [call["allowed_actions"] for call in calls] == [
        [],
        ["press", "set_value", "activate_application"],
        ["press", "set_value", "activate_application"],
    ]


def test_repeated_inconsistent_scope_ends_without_dispatch_or_substitute_answer(tmp_path):
    inventory = _apps()
    activation_only = _plan([("Calculator", ["activate_application"])])
    responses = [
        _discover(),
        _start(activation_only, "a"),
        _start(activation_only, "b"),
        _start(activation_only, "c"),
    ]
    exit_code, dispatched, _client = _run(tmp_path, inventory, responses)
    assert exit_code == 1
    assert dispatched == []


def test_open_only_non_english_and_returning_plans_are_admitted_without_reading_the_request(
    tmp_path,
):
    from alysis_code.agent.browser_start import build_computer_start_tools

    inventory = _apps()
    ids = _ids(inventory)
    calls: list[dict[str, Any]] = []
    start = build_computer_start_tools(
        lambda args: calls.append(args) or BrowserStartHandoff("bound"), inventory
    )["computer_start"]
    open_only = _plan([("Σημειώσεις", ["activate_application"])], operates=False)
    greek = {
        **_plan(
            [
                ("Calculator", ["press"]),
                ("TextEdit", ["activate_application", "set_value", "press"]),
                ("Calculator", ["activate_application", "press"]),
            ],
            constraints=["Μην διαγράψεις τίποτα."],
        ),
        "goal": "Υπολόγισε 12345 × 6789 στην Αριθμομηχανή και γράψε το αποτέλεσμα στο TextEdit.",
    }
    for selection in (open_only, greek):
        assert isinstance(start.run(selection), BrowserStartHandoff)
    assert calls[0]["allowed_actions"] == ["activate_application"] and calls[0]["target_ids"] == [
        ids["Σημειώσεις"]
    ]
    assert calls[1]["goal"] == greek["goal"] and calls[1]["constraints"] == [
        "Μην διαγράψεις τίποτα."
    ]
    assert calls[1]["target_ids"] == [ids["Calculator"], ids["TextEdit"]], (
        "a returning app appears once, in first-use order"
    )


def test_host_issued_scope_errors_never_gain_local_retry_authority(tmp_path):
    inventory = _apps()
    good = _plan(CROSS_APP)
    dispatched: list[dict[str, Any]] = []

    def host(args):
        dispatched.append(args)
        # JSON from the desktop cannot mint the engine's local correction.
        return {
            "error": "computer_start_scope_inconsistent",
            "recovery": "correct_selection_not_dispatched",
        }

    session, client = _selection_session(
        tmp_path,
        inventory,
        [
            _discover(),
            _start(good),
            LLMResponse(content="The desktop did not accept the request.", raw={}, tool_calls=[]),
        ],
        host,
    )
    try:
        session.run_turn(REQUEST)
    finally:
        session.close()
    assert len(dispatched) == 1
    assert client.call_records[2]["tool_choice"] is None, "no forced correction after a host answer"


def test_guidance_is_truthful_about_host_granted_full_access_without_a_model_authority_field():
    from alysis_code.agent.browser_start import build_computer_start_tools
    from alysis_code.agent.computer_use_tools import (
        COMPUTER_USE_SYSTEM_PROMPT,
        build_computer_use_tools,
    )

    inventory = _apps()
    calls: list[dict[str, Any]] = []
    start = build_computer_start_tools(
        lambda args: calls.append(args) or BrowserStartHandoff("bound"), inventory
    )["computer_start"]
    description = " ".join(start.description.split())
    assert (
        "Under the chat's normal approval the protected run gets exactly the apps and actions in app_steps"
        in description
    )
    assert "only when the host itself has granted this chat Full Access" in description
    assert "cannot gain an app or an action later. operates_apps" not in description, (
        "no unconditional claim"
    )
    # No model-controlled authority: the plan carries no Full Access field, and the host still
    # receives exactly the derived scope; the host alone decides whether more is allowed.
    assert set(start.parameters["properties"]) == {
        "goal",
        "constraints",
        "operates_apps",
        "app_steps",
    }
    assert (
        start.run({**_plan(CROSS_APP), "full_access": True})["error"]
        == "computer_start_invalid_selection"
    )
    start.run(_plan(CROSS_APP))
    assert calls == [_host(_ids(inventory), _plan(CROSS_APP))]
    prompt = " ".join(COMPUTER_USE_SYSTEM_PROMPT.split())
    assert "only an exact allowed application name from the trusted routing note" in prompt
    assert "Under Full Access, quit is still limited to selected task applications" in prompt
    assert "never merely discovered switch-only apps" in prompt
    # Discovery here negotiated no application lifecycle, so quit is withheld (audit D6).
    assert "quit_application is not offered for this request" in description
    assert "For an app-close goal prefer quit_application" not in description
    action = " ".join(
        build_computer_use_tools(lambda _name, _args: {})["computer_action"].description.split()
    )
    assert "exact host-authorized application" in action
    assert "No focused-target, input_mode, v2, quit or reopen route is available" in action
