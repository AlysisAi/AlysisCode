"""On-demand app labels with the original exact host admission contract."""

import copy
import json
import uuid

import pytest
from jsonschema import Draft202012Validator
from test_computer_discovery_recovery import (
    _discovered,
    _host,
    _inventory,
    _naming,
    _selection,
    _selection_session,
)

from alysis_code.agent.browser_start import BrowserStartHandoff, build_computer_start_tools
from alysis_code.llm.openai_compat import LLMResponse, ToolCall

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


def test_model_schema_is_independent_of_target_inventory_but_controller_keeps_all_labels():
    empty = build_computer_start_tools(lambda _: None, _inventory(0))["computer_start"]
    full = build_computer_start_tools(lambda _: None, _inventory(128))["computer_start"]

    # Scope actions still reflect native versus browser availability. Only app labels
    # belong in the discovery result, not every initial tool schema.
    def app(tool):
        return tool.as_openai_tool()["function"]["parameters"]["properties"]["app_steps"]["items"][
            "properties"
        ]["app"]

    assert app(empty) == app(full) == {"type": "string"}
    assert (
        len(full.parameters["properties"]["app_steps"]["items"]["properties"]["app"]["enum"]) == 128
    )


def test_projection_cannot_mutate_authority_and_unlisted_apps_never_reach_host():
    inventory = _inventory(128)
    calls = []
    tools = build_computer_start_tools(lambda args: calls.append(args), inventory)
    start = tools["computer_start"]
    original = copy.deepcopy(start.parameters)
    projected = start.as_openai_tool()
    projected["function"]["parameters"]["properties"]["app_steps"]["items"]["properties"]["app"][
        "enum"
    ] = ["foreign"]
    assert start.parameters == original
    wrong = _naming(_selection(inventory), "foreign")
    assert not Draft202012Validator(start.parameters).is_valid(wrong)
    assert start.run(wrong)["recovery"] == "correct_selection_not_dispatched"
    assert calls == []
    assert tools["computer_discover"].run({}) == _discovered(inventory)
    valid = _selection(inventory, 127)
    start.run(valid)
    assert calls == [_host(valid, inventory)]


@pytest.mark.parametrize(
    ("prompt", "actions"),
    [
        ("Open Calculator and calculate 12345 × 6789.", ["activate_application", "press"]),
        # The last target is installed, so even reading it requires opening it.
        ("Read the value shown in the selected app without changing it.", ["activate_application"]),
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
    first_schema = json.dumps(client.call_records[0]["tools"], ensure_ascii=False)
    assert all(
        target["target_id"] not in first_schema and target["name"] not in first_schema
        for target in inventory["targets"]
    )
    second = client.call_records[1]
    delivered = [
        json.loads(message["content"])
        for message in second["messages"]
        if message.get("role") == "tool"
    ]
    assert _discovered(inventory) in delivered, (
        "every target remains discoverable, including the last one"
    )
    assert host_calls == [_host(selection, inventory)], (
        "only the exact discovered selection crosses the host boundary"
    )


def test_labels_from_another_list_are_refused_without_hiding_current_discovery():
    first, second = _inventory(1), _inventory(1)
    second["targets"][0].update(target_id=str(uuid.uuid4()), name="Other 000")
    calls = []
    tools = build_computer_start_tools(lambda args: calls.append(args), second)
    assert (
        tools["computer_start"].run(_selection(first))["error"]
        == "computer_start_invalid_selection"
    )
    assert tools["computer_discover"].run({}) == _discovered(second)
    tools["computer_start"].run(_selection(second))
    assert calls == [_host(_selection(second), second)], (
        "a label resolves only to this request's exact host ID"
    )
