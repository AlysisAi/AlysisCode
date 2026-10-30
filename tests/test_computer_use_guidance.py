"""W9 F1: delivered wording and scripted production turns, never live-model acceptance."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from test_computer_discovery_recovery import _host, _inventory, _selection
from test_computer_interaction_v2 import action_result, call, done, image_state, state
from test_computer_use_capture_refusal import observation
from test_computer_use_host_refusal import refused
from test_computer_use_skill import _ordinary_session, _prompt_from_bridge, _RecordingClient
from test_computer_use_skill import isolated_paths as isolated_paths
from test_embedded_browser_visual import visual_cfg

from alysis_code import token_budget
from alysis_code.agent.browser_start import (
    BrowserStartHandoff,
    build_computer_start_tools,
    computer_plan_problem,
    computer_start_problem,
)
from alysis_code.agent.computer_use_tools import (
    COMPUTER_USE_SYSTEM_PROMPT,
    build_computer_use_tools,
)
from alysis_code.agent_loop import create_session
from alysis_code.cancellation import InteractiveCancellationToken
from alysis_code.skills.computer_use import computer_use_workflow_instructions
from alysis_code.surface.noop_surface import NoopSurface

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


@pytest.fixture(autouse=True)
def inert_tokenizer(monkeypatch):
    # These fixtures measure guidance/delivery, not tokenizer assets or speed.
    monkeypatch.setattr(token_budget, "_token_encoder", lambda: None)


def _guidance(prompt):
    assert prompt.startswith(COMPUTER_USE_SYSTEM_PROMPT)
    assert prompt.count(computer_use_workflow_instructions().strip()) == 1
    # The old blanket instruction contradicts the fixed prompt's automatic binding.
    assert (
        "After a state change, app/window transition, stale target, or takeover/Resume, obtain"
        not in prompt
    )
    assert "reuse that post-state without another computer_state call" in prompt
    for safeguard in (
        "independent/final verification",
        "insufficient or uncertain",
        "fresh image",
        "window_selection_required",
        "capture_refusal_limit",
        "no tool calls",
        "Never resume by replaying queued actions",
        "one Computer Use tool call",
    ):
        assert safeguard in prompt


def test_guidance_delivered_by_bridge_resolves_post_state_conflict(tmp_path, monkeypatch):
    _guidance(_prompt_from_bridge(tmp_path, monkeypatch))


@pytest.mark.parametrize("v2", [False, True])
def test_guidance_descriptions_agree_with_binding_and_evidence_requirements(v2):
    tools = build_computer_use_tools(lambda *_: {}, interaction_v2=v2)
    for name in ("computer_state", "computer_action"):
        description = tools[name].description
        assert "sufficient current post-state" in description
        assert "independent/final verification" in description
        assert "fresh image" in description


def _example(description, name):
    marker = f"Example {name}: "
    assert marker in description, "scope planning needs executable structural examples"
    return json.JSONDecoder().raw_decode(description.split(marker, 1)[1])[0]


def test_guidance_start_examples_validate_all_scope_relationships():
    inventory = _inventory()
    description = build_computer_start_tools(lambda *_: {}, inventory)["computer_start"].description
    assert "including navigation needed to read" in description
    assert "any action other than activate_application" in description
    for name, actions, operates in (
        ("observe", [[]], False),
        ("open", [["activate_application"]], False),
        ("navigate", [["press"]], True),
        ("switch", [[], ["activate_application", "press"]], True),
    ):
        value = _example(description, name)
        assert [step["actions"] for step in value["app_steps"]] == actions
        assert value["operates_apps"] is operates
        labels = {step["app"] for step in value["app_steps"]}
        targets = {label: {"availability": "running"} for label in labels}
        if name == "open":
            targets[value["app_steps"][0]["app"]]["availability"] = "installed"
        assert computer_start_problem(value, labels) is None
        assert computer_plan_problem(value, targets) is None
        assert value["constraints"], "examples must preserve task constraints"


def _protected(root, monkeypatch, host, responses):
    prompt = _prompt_from_bridge(root, monkeypatch)
    session = create_session(
        cfg=visual_cfg(),
        root=root,
        mode="auto",
        yes=True,
        max_steps=15,
        no_log=True,
        api_key_override="fixture-key",
        surface=NoopSurface(),
        sensitive_media=True,
        trusted_system_prompt_override=prompt,
        computer_use_host_request=host,
        computer_interaction_v2=True,
        computer_observation_available=True,
    )
    client = _RecordingClient(responses)
    session.client = client
    return session, client


@pytest.mark.parametrize("case", ["adequate", "inadequate", "image_needed", "optional_read"])
def test_next_production_request_has_guidance_and_post_state(tmp_path, monkeypatch, case):
    calls = []

    def host(name, args):
        calls.append((name, dict(args)))
        if args.get("include_image"):
            return image_state()
        value = state(len(calls))
        value["elements"] = [
            {"id": "next-page", "role": "AXButton", "name": "Next page", "actions": ["press"]},
            {
                "id": "page-text",
                "role": "AXStaticText",
                "name": "Synthetic page 1"
                if len(calls) == 1
                else "Synthetic loading"
                if case == "inadequate" and len(calls) == 2
                else "Synthetic page 2",
            },
        ]
        if name == "computer_action":
            assert args["snapshot_id"] == "snapshot-1"
            return action_result(args, value, effect="observed_change")
        return value

    responses = [
        call("computer_state", {}, 1),
        call(
            "computer_action",
            {
                "action": "press",
                "target_type": "semantic",
                "element_id": "next-page",
            },
            2,
        ),
    ]
    if case != "adequate":
        responses.append(
            call("computer_state", {"include_image": True} if case == "image_needed" else {}, 3)
        )
    responses.append(done())
    session, client = _protected(tmp_path, monkeypatch, host, responses)
    try:
        assert session.run_turn("Read the synthetic viewer; inspect its image only if needed.") == 0
        assert session.last_turn_outcome["outcome"] == "completed_unverified"
    finally:
        session.close()
    request = client.call_records[2]
    prompt = next(m["content"] for m in request["messages"] if m["role"] == "system")
    _guidance(prompt)
    outputs = [json.loads(m["content"]) for m in request["messages"] if m.get("role") == "tool"]
    assert outputs, request["messages"]
    action_output = outputs[-1]
    assert action_output["state"]["snapshot_id"] == "snapshot-2"
    assert ("Synthetic loading" if case == "inadequate" else "Synthetic page 2") in json.dumps(
        action_output
    )
    assert "data:image" not in json.dumps(request)
    for tool in request["tools"]:
        if tool["function"]["name"] in {"computer_state", "computer_action"}:
            assert "sufficient current post-state" in tool["function"]["description"]
    assert [name for name, _ in calls] == ["computer_state", "computer_action"] + (
        [] if case == "adequate" else ["computer_state"]
    )
    assert client.calls == 3 + (case != "adequate")
    assert sum(args.get("include_image") is True for _, args in calls) == (case == "image_needed")
    if case == "image_needed":
        assert "data:image/png;base64," in json.dumps(client.call_records[3]["messages"])
    elif case == "inadequate":
        assert "Synthetic page 2" in json.dumps(client.call_records[3]["messages"])


def _scope_pair(family):
    inventory = _inventory()
    good = _selection(inventory, actions=["press"])
    bad = deepcopy(good)
    if family == "operates_true_without_input":
        bad["app_steps"][0]["actions"] = []
    elif family == "operates_false_with_input":
        bad["operates_apps"] = False
    elif family == "app_switch_without_activation":
        good = _selection(inventory, actions=[])
        good["app_steps"].append(
            {"app": inventory["targets"][1]["name"], "actions": ["activate_application"]}
        )
        bad = deepcopy(good)
        bad["app_steps"][1]["actions"] = []
    else:
        good = _selection(inventory, 1, ["activate_application"])
        bad = _selection(inventory, 1, [])
    return inventory, bad, good


@pytest.mark.parametrize(
    "family",
    [
        "operates_true_without_input",
        "operates_false_with_input",
        "app_switch_without_activation",
        "initial_open_without_activation",
    ],
)
@pytest.mark.parametrize("correct", [False, True])
def test_scope_refusal_families_keep_bounded_correction(tmp_path, family, correct):
    inventory, bad, good = _scope_pair(family)
    calls = []
    responses = [
        call("computer_discover", {}, 1),
        call("computer_start", bad, 2),
        call("computer_start", good if correct else bad, 3),
    ]
    if not correct:
        responses.append(call("computer_start", bad, 4))
    session, client = _ordinary_session(
        tmp_path,
        inventory,
        responses,
        lambda args: calls.append(args) or BrowserStartHandoff("fixture"),
    )
    try:
        assert session.run_turn(
            "Read the requested synthetic app. Do not change anything else."
        ) == (0 if correct else 1)
    finally:
        session.close()
    request = client.call_records[2]
    results = [json.loads(m["content"]) for m in request["messages"] if m.get("role") == "tool"]
    assert any(r.get("error") == "computer_start_scope_inconsistent" for r in results)
    assert calls == ([_host(good, inventory)] if correct else [])
    assert client.calls == (3 if correct else 4)
    description = next(
        t["function"]["description"]
        for t in request["tools"]
        if t["function"]["name"] == "computer_start"
    )
    assert "including navigation needed to read" in description


@pytest.mark.parametrize("control", ["capture_limit", "reobserve", "stop"])
def test_delivered_guidance_preserves_terminal_and_recovery_controls(
    tmp_path, monkeypatch, control
):
    calls = []
    token = InteractiveCancellationToken()

    def host(name, args):
        calls.append((name, dict(args)))
        if control == "capture_limit":
            return observation()
        if control == "stop":
            token.cancel()
        return refused(authority="valid_for_reobserve")

    responses = [call("computer_state", {}, 1), call("computer_state", {}, 2), done()]
    session, client = _protected(tmp_path, monkeypatch, host, responses)
    try:
        if control == "stop":
            with pytest.raises(KeyboardInterrupt):
                session.run_turn("Read the synthetic app.", cancellation_token=token)
        else:
            assert session.run_turn("Read the synthetic app.") == 0
            assert session.last_turn_outcome["outcome"] == "blocked"
    finally:
        session.close()
    # Terminal capture refusal permits only a report; W6b permits one re-observation.
    assert len(calls) == (2 if control == "reobserve" else 1)
    assert client.calls == (1 if control == "stop" else 2)
    _guidance(
        next(m["content"] for m in client.call_records[0]["messages"] if m["role"] == "system")
    )
