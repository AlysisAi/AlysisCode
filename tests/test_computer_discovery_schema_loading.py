"""On-demand target data with the original exact host admission contract."""

import copy
import json
import uuid

import pytest
from jsonschema import Draft202012Validator
from test_computer_discovery_recovery import _inventory, _selection, _selection_session

from alysis_code.agent.browser_start import BrowserStartHandoff, build_computer_start_tools
from alysis_code.llm.openai_compat import LLMResponse, ToolCall


def test_model_schema_is_independent_of_target_inventory_but_controller_keeps_all_ids():
    empty = build_computer_start_tools(lambda _: None, _inventory(0))["computer_start"]
    full = build_computer_start_tools(lambda _: None, _inventory(128))["computer_start"]
    # Scope actions still reflect native versus browser availability. Only target
    # identities belong in the discovery result, not every initial tool schema.
    empty_ids = empty.as_openai_tool()["function"]["parameters"]["properties"]["target_ids"]
    full_ids = full.as_openai_tool()["function"]["parameters"]["properties"]["target_ids"]
    assert empty_ids == full_ids
    assert full_ids["items"] == {"type": "string"}
    assert len(full.parameters["properties"]["target_ids"]["items"]["enum"]) == 128


def test_projection_cannot_mutate_authority_and_undiscovered_ids_never_reach_host():
    inventory = _inventory(128)
    calls = []
    tools = build_computer_start_tools(lambda args: calls.append(args), inventory)
    start = tools["computer_start"]
    original = copy.deepcopy(start.parameters)
    projected = start.as_openai_tool()
    projected["function"]["parameters"]["properties"]["target_ids"]["items"]["enum"] = ["foreign"]
    assert start.parameters == original
    wrong = {**_selection(inventory), "target_ids": [str(uuid.uuid4())]}
    assert not Draft202012Validator(start.parameters).is_valid(wrong)
    assert start.run(wrong)["recovery"] == "correct_selection_not_dispatched"
    assert calls == []
    assert tools["computer_discover"].run({}) == inventory
    valid = _selection(inventory, 127)
    start.run(valid)
    assert calls == [valid]


@pytest.mark.parametrize(
    ("prompt", "actions"),
    [
        ("Open Calculator and calculate 12345 × 6789.", ["activate_application", "press"]),
        ("Read the value shown in the selected app without changing it.", []),
        (
            "Άνοιξε τις Σημειώσεις και βάλε τη λέξη Hello στο πεδίο κειμένου.",
            ["activate_application", "set_value"],
        ),
    ],
)
def test_natural_request_can_discover_previously_unexposed_last_target(tmp_path, prompt, actions):
    inventory = _inventory(128)
    selection = {**_selection(inventory, 127, actions), "goal": prompt}
    host_calls = []
    responses = [
        LLMResponse(
            content="",
            raw={},
            tool_calls=[ToolCall(id="discover", name="computer_discover", arguments={})],
        ),
        LLMResponse(
            content="",
            raw={},
            tool_calls=[ToolCall(id="select", name="computer_start", arguments=selection)],
        ),
    ]
    session, client = _selection_session(
        tmp_path,
        inventory,
        responses,
        lambda args: host_calls.append(copy.deepcopy(args)) or BrowserStartHandoff("bound"),
    )
    try:
        assert session.run_turn(prompt) == 0
    finally:
        session.close()
    first_schema = json.dumps(client.call_records[0]["tools"])
    assert all(target["target_id"] not in first_schema for target in inventory["targets"])
    second = client.call_records[1]
    delivered = [
        json.loads(message["content"])
        for message in second["messages"]
        if message.get("role") == "tool"
    ]
    assert inventory in delivered, "every target remains discoverable, including the last one"
    assert host_calls == [selection], (
        "only the exact discovered selection crosses the host boundary"
    )


def test_ids_from_another_turn_are_refused_without_hiding_current_discovery():
    first, second = _inventory(1), _inventory(1)
    second["targets"][0]["target_id"] = str(uuid.uuid4())
    calls = []
    tools = build_computer_start_tools(lambda args: calls.append(args), second)
    assert (
        tools["computer_start"].run(_selection(first))["error"]
        == "computer_start_invalid_selection"
    )
    assert tools["computer_discover"].run({}) == second
    tools["computer_start"].run(_selection(second))
    assert calls == [_selection(second)]
