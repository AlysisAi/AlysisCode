"""Replay of operation 880f1d10: repeated actions in app_steps and the rediscovery loop.

The captured plans named the right apps but repeated actions inside a step (a sequence, not a set),
and after the second correction the model rediscovered the same list nine times. Requests go through
the OpenAI Responses adapter mock in test_computer_selection_captured_replay (every raw request kept,
forced choices asserted per turn). Controlled evidence only, not provider acceptance.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from test_computer_selection_captured_replay import forced, provider_tool_outputs, run

from alysis_code.agent.browser_start import (
    COMPUTER_ACTIONS,
    BrowserStartHandoff,
    build_computer_start_tools,
)

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/computer_selection/op_880f1d10.json").read_text()
)
INVENTORY = FIXTURE["inventory"]
IDS = {target["name"]: target["target_id"] for target in INVENTORY["targets"]}
FIRST, SECOND, THIRD = FIXTURE["computer_start_calls"]
DISCOVER_NOW = "Call computer_discover now"
CORRECTION = "Computer Use selection correction"


def _host(plan: dict[str, Any]) -> dict[str, Any]:
    steps = plan["app_steps"]
    return {
        "target_ids": [IDS[app] for app in dict.fromkeys(step["app"] for step in steps)],
        "allowed_actions": [
            a for a in COMPUTER_ACTIONS if any(a in step["actions"] for step in steps)
        ],
        "goal": plan["goal"],
        "constraints": plan["constraints"],
    }


@pytest.mark.parametrize(
    ("call", "distinct"),
    [
        (FIRST, ["press", "set_value", "activate_application"]),
        (SECOND, ["press", "set_value", "activate_application"]),
        (THIRD, ["press", "focus", "type", "set_value", "key", "activate_application"]),
    ],
)
def test_each_captured_plan_is_admitted_with_exactly_its_distinct_actions(call, distinct):
    assert any(len(step["actions"]) != len(set(step["actions"])) for step in call["app_steps"]), (
        "captured repeats"
    )
    calls: list[dict[str, Any]] = []
    start = build_computer_start_tools(
        lambda s: calls.append(s) or BrowserStartHandoff("bound"), INVENTORY
    )["computer_start"]
    assert isinstance(start.run(copy.deepcopy(call)), BrowserStartHandoff)
    assert calls == [
        {
            "target_ids": [IDS["Calculator"], IDS["Finder"], IDS["TextEdit"]],
            "allowed_actions": distinct,
            "goal": call["goal"],
            "constraints": call["constraints"],
        }
    ], "repeats never widen the host scope"


def test_the_captured_first_call_reaches_one_exact_handoff_through_the_responses_adapter(tmp_path):
    exit_code, dispatched, log = run(
        tmp_path, INVENTORY, [("computer_discover", {}), ("computer_start", FIRST)]
    )
    assert exit_code == 0 and len(log["turns"]) == 2
    assert dispatched == [_host(FIRST)]
    assert [forced(body) for body in log["turns"]] == [None, None]
    listed = {app["app"]: app for app in provider_tool_outputs(log["turns"][1])[0]["targets"]}
    assert len(listed) == 107 and listed["TextEdit"].get("availability") == "no_window"


def _unlisted(call: dict[str, Any]) -> dict[str, Any]:
    """The captured plan with its TextEdit label replaced by a near miss that stays invalid."""
    bad = copy.deepcopy(call)
    bad["app_steps"][2]["app"] = "Text Edit"
    return bad


def test_after_the_list_is_delivered_again_selection_is_required_and_no_stale_directive_remains(
    tmp_path,
):
    starts = iter([_unlisted(FIRST), _unlisted(SECOND), _unlisted(THIRD)])
    # The captured call order: discover, start, discover, start, nine discovers, start.
    script = [
        (name, next(starts)) if name == "computer_start" else (name, {})
        for name in FIXTURE["tool_call_names"]
    ]
    # The captured turn used 14 steps; give the replay room for all of them.
    exit_code, dispatched, log = run(tmp_path, INVENTORY, script, max_steps=20)
    assert exit_code == 1 and dispatched == []
    assert len(log["turns"]) == len(script) == 14, (
        "bounded: the third invalid start ends the request"
    )
    choices = [forced(body) for body in log["turns"]]
    # Each correction delivers the request's list again once; after that delivery only a corrected
    # selection is asked for, until one is made. This mock ignores tool_choice, so it replays the
    # captured extra discoveries; a provider that honours the choice could not.
    assert (
        choices
        == [None, None, "computer_discover", "computer_start", "computer_discover"]
        + ["computer_start"] * 9
    )
    for body, choice in zip(log["turns"], choices, strict=True):
        text = json.dumps(body)
        assert text.count(CORRECTION) <= 1, (
            "one current correction directive, never a stale one beside it"
        )
        assert (DISCOVER_NOW in text) == (choice == "computer_discover"), (
            "no 'discover now' after the list was delivered"
        )
    corrections = [
        result
        for body in log["turns"]
        for result in provider_tool_outputs(body)[-1:]
        if "error" in result
    ]
    assert corrections and all(
        "Step 3's app is not one of the listed app labels" in c["message"] for c in corrections
    )
    assert all(len(c["available_targets"]) == 107 for c in corrections)


def _plan(steps: list[tuple[str, list[Any]]], *, operates: bool = True) -> dict[str, Any]:
    return {
        "goal": "Fixture goal.",
        "constraints": ["Fixture constraint."],
        "operates_apps": operates,
        "app_steps": [{"app": app, "actions": actions} for app, actions in steps],
    }


def test_repeats_are_one_declaration_within_the_existing_size_bound_and_every_other_check_stays():
    from jsonschema import Draft202012Validator

    calls: list[dict[str, Any]] = []
    start = build_computer_start_tools(
        lambda s: calls.append(s) or BrowserStartHandoff("bound"), INVENTORY
    )["computer_start"]
    actions_schema = start.parameters["properties"]["app_steps"]["items"]["properties"]["actions"]
    assert actions_schema["maxItems"] == len(COMPUTER_ACTIONS) == 11, (
        "the raw size bound includes each supported action once"
    )
    validator = Draft202012Validator(start.parameters)
    at_limit = _plan([("Calculator", ["press"] * 11)])
    over_limit = _plan([("Calculator", ["press"] * 12)])
    assert validator.is_valid(at_limit) and not validator.is_valid(over_limit)
    over = start.run(over_limit)
    assert (
        over["error"] == "computer_start_invalid_selection"
        and "at most 11 action names" in over["message"]
    )
    assert isinstance(start.run(at_limit), BrowserStartHandoff)
    assert calls == [
        {
            "target_ids": [IDS["Calculator"]],
            "allowed_actions": ["press"],
            "goal": "Fixture goal.",
            "constraints": ["Fixture constraint."],
        }
    ], "ten presses grant press once, nothing else"
    refusals = {
        "unknown action, repeated": (
            _plan([("Calculator", ["press", "tap", "tap"])]),
            "computer_start_invalid_selection",
            "Step 1 names an unknown action",
        ),
        "non-string action": (
            _plan([("Calculator", ["press", 1])]),
            "computer_start_invalid_selection",
            "Step 1 names an unknown action",
        ),
        "browser-only action on a native app": (
            _plan([("Calculator", ["press", "history_back", "history_back"])]),
            "computer_start_unsupported_native_actions",
            "history_back",
        ),
        "switch without activation": (
            _plan([("Calculator", ["press", "press"]), ("Finder", ["press", "press"])]),
            "computer_start_scope_inconsistent",
            "activate_application",
        ),
        "reads but repeats input": (
            _plan([("Calculator", ["press", "press"])], operates=False),
            "computer_start_scope_inconsistent",
            "operates_apps is false",
        ),
        "windowless app not opened": (
            _plan([("TextEdit", ["set_value", "set_value"])]),
            "computer_start_scope_inconsistent",
            "no_window",
        ),
        "retired fields": (
            {**_plan([("Calculator", ["press"])]), "allowed_actions": ["press"]},
            "computer_start_invalid_selection",
            "Do not send target_ids or allowed_actions",
        ),
        "unlisted app": (
            _plan([("Calculator", ["press"]), ("Text Edit", ["activate_application"])]),
            "computer_start_invalid_selection",
            "Step 2's app is not one of the listed app labels",
        ),
    }
    for label, (plan, code, reason) in refusals.items():
        result = start.run(plan)
        assert result["error"] == code, label
        assert reason in result["message"], label
        assert result["recovery"] == "correct_selection_not_dispatched", label
    assert len(calls) == 1, "no refused plan reaches the host"


def test_only_the_controllers_own_directive_object_is_replaced():
    from alysis_code.agent.turn.core import _drop_selection_directive

    directive = "Computer Use selection correction: " + "x" * 3
    same_text_elsewhere = "".join(["Computer Use selection correction: ", "xxx"])
    prompts = ["unrelated turn prompt", same_text_elsewhere, directive, "another prompt"]
    assert same_text_elsewhere == directive and same_text_elsewhere is not directive
    _drop_selection_directive(prompts, directive)
    assert prompts == ["unrelated turn prompt", same_text_elsewhere, "another prompt"]
    assert prompts[1] is same_text_elsewhere
    _drop_selection_directive(prompts, None)
    _drop_selection_directive(prompts, "never added")
    assert len(prompts) == 3


def test_a_host_denial_after_recovery_leaves_no_forced_start_or_stale_directive(tmp_path):
    from test_computer_selection_captured_replay import REQUEST, responses_session

    from alysis_code.agent.turn.core import _COMPUTER_SELECTION_CORRECTION_TAIL

    owned_text = (
        "Computer Use selection correction: nothing was started. This request's captured app list was "
        "delivered again; do not request it again. Call computer_start now with app labels copied exactly "
        "from it. " + _COMPUTER_SELECTION_CORRECTION_TAIL
    )
    unrelated = "Unrelated turn prompt that must survive."
    host_calls: list[dict[str, Any]] = []

    def host(selection):
        host_calls.append(copy.deepcopy(selection))
        return {
            "error": "computer_use_activation_required"
        }  # a host refusal: no local retry authority

    script = [
        ("computer_discover", {}),
        ("computer_start", _unlisted(FIRST)),
        ("computer_discover", {}),
        ("computer_start", FIRST),
        "The selected app could not be started.",
    ]
    session, log = responses_session(tmp_path, INVENTORY, script, host, max_steps=12)
    try:
        # An equal-text prompt the controller does not own must outlive the controller's directive.
        session.run_turn(REQUEST, ephemeral_system_messages=[unrelated, owned_text])
    finally:
        session.close()
    assert len(log["turns"]) == len(script)
    assert host_calls == [_host(FIRST)], "the corrected selection reached the host exactly once"
    assert [forced(body) for body in log["turns"]] == [
        None,
        None,
        "computer_discover",
        "computer_start",
        None,
    ]
    texts = [json.dumps(body) for body in log["turns"]]
    owned = json.dumps(owned_text)[1:-1]
    assert texts[3].count(owned) == 2, (
        "the controller's directive beside the equal-text unrelated prompt"
    )
    after = texts[4]
    assert after.count(owned) == 1, (
        "only the non-owned equal-text prompt remains after the host answered"
    )
    assert DISCOVER_NOW not in after and json.dumps(unrelated)[1:-1] in after
    denial = provider_tool_outputs(log["turns"][4])[-1]
    assert denial["error"] == "computer_use_activation_required" and "recovery" not in denial
