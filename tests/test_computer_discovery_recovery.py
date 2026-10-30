"""Discovery inventory delivery and truthful stale-selection recovery (no real apps or providers)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from test_computer_use_recovery import _ScriptedComputerUseClient

from alysis_code.agent.browser_start import (
    COMPUTER_ACTIONS,
    COMPUTER_START_REFUSALS,
    BrowserStartHandoff,
    ComputerDiscoveryTerminal,
    ComputerSelectionCorrection,
    build_computer_start_tools,
)
from alysis_code.agent.computer_use_tools import (
    COMPUTER_USE_SYSTEM_PROMPT,
    build_computer_use_tools,
)
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.surface.noop_surface import NoopSurface

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


def _results_for_tool(messages, name):
    # Production replaces provider-chosen Computer Use call IDs with opaque host IDs.
    # Match the actual assistant/result pairs, preserving that privacy boundary.
    calls = {
        call["id"]: call["function"]["name"]
        for message in messages
        for call in message.get("tool_calls", [])
    }
    return [
        json.loads(message["content"])
        for message in messages
        if message.get("role") == "tool" and calls.get(message.get("tool_call_id")) == name
    ]


def _inventory(count: int = 2) -> dict[str, Any]:
    names = ["Σημειώσεις", "研究", "Calculadora", "Notes"]
    return {
        "version": 1,
        "targets": [
            {
                "target_id": str(uuid.UUID(int=index + 1, version=4)),
                "name": f"{names[index % len(names)]} {index:03d}",
                "route": "native",
                **({"availability": "installed"} if index % 2 else {}),
            }
            for index in range(count)
        ],
    }


def _discovered(inventory: dict[str, Any]) -> dict[str, Any]:
    """Exact labels and read-only host-issued identity references, without authority."""
    return {
        "version": 1,
        "targets": [
            {
                "app": t["name"],
                "application_ref": t["target_id"],
                "route": t["route"],
                **({"availability": t["availability"]} if t.get("availability") else {}),
            }
            for t in inventory["targets"]
        ],
    }


def _host(selection: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    """The exact host selection a plan implies: its apps' IDs in first-use order and its actions."""
    ids = {t["name"]: t["target_id"] for t in inventory["targets"]}
    steps = selection["app_steps"]
    return {
        "target_ids": [ids[app] for app in dict.fromkeys(step["app"] for step in steps)],
        "allowed_actions": [
            a for a in COMPUTER_ACTIONS if any(a in step["actions"] for step in steps)
        ],
        "goal": selection["goal"],
        "constraints": selection["constraints"],
    }


def _selection(
    inventory: dict[str, Any], index: int = 0, actions: list[str] | None = None
) -> dict[str, Any]:
    target = inventory["targets"][index]
    if actions is None:
        # An installed app must be opened by the step that uses it.
        actions = (
            ["activate_application", "press"]
            if target.get("availability") == "installed"
            else ["press"]
        )
    return {
        "goal": "Read the current value.",
        "constraints": ["Do not change anything else."],
        "operates_apps": bool(set(actions) - {"activate_application"}),
        "app_steps": [{"app": target["name"], "actions": list(actions)}],
    }


def _naming(selection: dict[str, Any], app: str) -> dict[str, Any]:
    """The same plan referring to a different app label."""
    return {
        **selection,
        "app_steps": [{**selection["app_steps"][0], "app": app}, *selection["app_steps"][1:]],
    }


@pytest.mark.parametrize("tool_name", ["computer_discover", "computer_start"])
def test_empty_inventory_explains_setup_without_entering_selection_recovery(tool_name: str):
    calls: list[Any] = []
    tools = build_computer_start_tools(lambda selection: calls.append(selection), _inventory(0))
    arguments = {} if tool_name == "computer_discover" else _selection(_inventory())

    result = tools[tool_name].run(arguments)
    assert result["error"] == "computer_discovery_unavailable"
    assert result["fresh_turn_required"] is True
    assert "Settings > Computer use > Any App" in result["message"]
    assert "does not prove" in result["message"]
    assert "Do not retry" in result["message"]
    assert not isinstance(result, ComputerSelectionCorrection)
    assert "recovery" not in result, "an empty list cannot be corrected by forcing a selection"
    assert calls == []

    result["message"] = "mutated consumer copy"
    assert tools[tool_name].run(arguments)["message"] != result["message"]
    assert calls == []


def test_empty_discovery_ends_without_another_provider_call_or_dispatch(tmp_path: Path):
    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                raw={},
                tool_calls=[ToolCall(id="empty-list", name="computer_discover", arguments={})],
            ),
            LLMResponse(
                content="App control is unavailable. Check Any App setup before trying again.",
                raw={},
                tool_calls=[],
            ),
        ]
    )
    calls: list[Any] = []
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=3,
        no_log=True,
        api_key_override="fixture",
        surface=NoopSurface(),
        computer_discovery=_inventory(0),
        browser_start_host_request=lambda arguments: calls.append(arguments),
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Open the requested app.") == 1
    finally:
        session.close()
    assert len(client.call_records) == 1
    assert calls == []
    final = session.messages[-1]["content"]
    assert "No Computer Use session was started" in final
    assert "send your request again" in final


def test_a_target_that_changed_after_discovery_requires_a_fresh_request_without_claiming_rediscovery():
    inventory = _inventory()
    host_calls: list[dict[str, Any]] = []

    def host(selection: dict[str, Any]) -> Any:
        host_calls.append(selection)
        # The desktop host revalidates the selection against current processes and installations.
        return {"error": "computer_use_target_stale"}

    tools = build_computer_start_tools(host, inventory)
    assert tools["computer_discover"].run({}) == _discovered(inventory)

    refused = tools["computer_start"].run(_selection(inventory))
    assert refused["error"] == "computer_use_target_stale"
    assert refused["fresh_turn_required"] is True
    assert "send the request again" in refused["message"]
    assert "rediscovered" in refused["message"], "the refusal says no rediscovery happened"
    assert len(host_calls) == 1

    # The captured inventory is never served again as if it were fresh.
    rediscovered = tools["computer_discover"].run({})
    assert rediscovered["error"] == "computer_discovery_stale"
    assert rediscovered["fresh_turn_required"] is True
    assert "targets" not in rediscovered
    assert inventory["targets"][0]["name"] not in json.dumps(rediscovered)

    # No further selection reaches the host in this request, for any captured target.
    for index in range(len(inventory["targets"])):
        again = tools["computer_start"].run(_selection(inventory, index))
        assert again["fresh_turn_required"] is True
    assert len(host_calls) == 1


def test_an_unfinished_host_scan_is_never_reported_as_a_missing_app():
    inventory = _inventory()
    tools = build_computer_start_tools(
        lambda _s: {"error": "computer_use_inventory_unavailable"}, inventory
    )
    refused = tools["computer_start"].run(_selection(inventory, 1, ["activate_application"]))
    assert refused["error"] == "computer_use_inventory_unavailable"
    assert refused["fresh_turn_required"] is True
    assert "Do not say the app is missing or not installed." in refused["message"]
    assert tools["computer_discover"].run({})["error"] == "computer_discovery_stale"


def test_correctable_refusals_explain_the_fix_and_keep_the_inventory_usable():
    inventory = _inventory()
    answers = iter([{"error": "computer_use_activation_required"}, BrowserStartHandoff("bound")])
    calls: list[dict[str, Any]] = []
    tools = build_computer_start_tools(lambda s: calls.append(s) or next(answers), inventory)
    # A plan that uses an installed app without opening it is corrected before the host.
    unopened = tools["computer_start"].run(_selection(inventory, 1, ["press"]))
    assert (
        unopened["error"] == "computer_start_scope_inconsistent"
        and "activate_application" in unopened["message"]
    )
    assert calls == []
    # The host's own refusal codes still map to the same explanation.
    refused = tools["computer_start"].run(_selection(inventory, 1))
    assert refused == {
        "error": "computer_use_activation_required",
        "message": COMPUTER_START_REFUSALS["computer_use_activation_required"],
    }
    assert "activate_application" in refused["message"]
    assert tools["computer_discover"].run({}) == _discovered(inventory)
    assert isinstance(
        tools["computer_start"].run(_selection(inventory, 1, ["activate_application", "press"])),
        BrowserStartHandoff,
    )
    assert len(calls) == 2

    invalid = tools["computer_start"].run(_naming(_selection(inventory), "com.example.app"))
    assert invalid["error"] == "computer_start_invalid_selection"
    assert "copied exactly" in invalid["message"]
    assert len(calls) == 2, "an invalid selection never reaches the host"

    # Unknown host codes pass through unchanged and never gain recovery text.
    passthrough = build_computer_start_tools(
        lambda _s: {"error": "computer_use_site_blocked"}, inventory
    )
    assert passthrough["computer_start"].run(_selection(inventory)) == {
        "error": "computer_use_site_blocked"
    }


def test_the_whole_host_bounded_inventory_reaches_the_model_without_a_truncated_preview(
    tmp_path: Path,
):
    inventory = _inventory(
        128
    )  # the host's cap; the last target sits far beyond a 2000-char preview
    last_app = inventory["targets"][-1]["name"]
    assert len(json.dumps(inventory)) > 6000
    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                raw={},
                tool_calls=[ToolCall(id="discover-1", name="computer_discover", arguments={})],
            ),
            LLMResponse(content="Listed.", raw={}, tool_calls=[]),
        ]
    )
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=3,
        no_log=False,
        api_key_override="fixture",
        surface=NoopSurface(),
        computer_discovery=inventory,
        browser_start_host_request=lambda _args: BrowserStartHandoff("unused"),
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Which apps can you see?") == 0
    finally:
        session.close()
    tool_messages = [
        message for message in client.call_records[1]["messages"] if message.get("role") == "tool"
    ]
    assert tool_messages, "the discovery result was returned to the model"
    content = str(tool_messages[-1]["content"])
    assert last_app in content, "the last selectable target is not lost to a preview"
    assert "artifact" not in content and "Truncated" not in content
    assert json.loads(content) == _discovered(inventory)
    assert [t["application_ref"] for t in json.loads(content)["targets"]] == [
        t["target_id"] for t in inventory["targets"]
    ], "identity references are read-only; the host selection still derives IDs from exact labels"


def test_local_background_input_refusal_reobserves_then_continues_with_a_semantic_action(
    tmp_path: Path,
):
    host_calls: list[tuple[str, dict[str, Any]]] = []

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, dict(arguments)))
        if name == "computer_state":
            return {
                "snapshot_id": f"state-{len(host_calls)}",
                "target": {"bundle_id": "org.example.notes"},
            }
        if arguments.get("action") in {"type", "key", "scroll", "drag"}:
            return {
                "error": "background_synthetic_input_unsupported",
                "message": "Keyboard, pointer, scroll and drag input is not delivered while Alysis works in the background; nothing was sent.",
            }
        return {
            "ok": True,
            "receipt": {"action": arguments.get("action")},
            "state": {
                "snapshot_id": f"state-{len(host_calls)}",
                "target": {"bundle_id": "org.example.notes"},
            },
        }

    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                raw={},
                tool_calls=[ToolCall(id="s1", name="computer_state", arguments={})],
            ),
            LLMResponse(
                content="",
                raw={},
                tool_calls=[
                    ToolCall(
                        id="t1",
                        name="computer_action",
                        arguments={"action": "type", "element_id": "field", "text": "Σημείωση"},
                    )
                ],
            ),
            LLMResponse(
                content="",
                raw={},
                tool_calls=[ToolCall(id="s2", name="computer_state", arguments={})],
            ),
            LLMResponse(
                content="",
                raw={},
                tool_calls=[
                    ToolCall(
                        id="v1",
                        name="computer_action",
                        arguments={
                            "action": "set_value",
                            "element_id": "field",
                            "text": "Σημείωση",
                        },
                    )
                ],
            ),
            LLMResponse(
                content="Entered the note; not independently verified.", raw={}, tool_calls=[]
            ),
        ]
    )
    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=True,
        api_key_override="override-key",
        surface=NoopSurface(),
        verification_enabled=False,
        sensitive_media=True,
        computer_use_host_request=host_request,
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Write Σημείωση in the note field.") == 0
    finally:
        session.close()
    assert [name for name, _ in host_calls] == [
        "computer_state",
        "computer_state",
        "computer_action",
    ]
    assert host_calls[-1][1]["action"] == "set_value"
    recovery = [
        str(m.get("content") or "")
        for m in client.call_records[2]["messages"]
        if m.get("role") == "system"
    ]
    assert any(
        "unavailable for the current observation" in text
        and "set_value" in text
        and "hand off the desktop" in text
        and "Never silently switch" in text
        for text in recovery
    )
    assert client.call_records[2]["tool_choice"]["function"]["name"] == "computer_state"


def test_local_background_input_refusals_are_bounded_and_never_dispatch(tmp_path: Path):
    host_calls: list[str] = []

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append(name)
        if name == "computer_state":
            return {"snapshot_id": f"state-{len(host_calls)}"}
        return {"error": "background_synthetic_input_unsupported", "message": "nothing was sent"}

    responses: list[LLMResponse] = []
    for index in range(3):
        responses += [
            LLMResponse(
                content="",
                raw={},
                tool_calls=[ToolCall(id=f"s{index}", name="computer_state", arguments={})],
            ),
            LLMResponse(
                content="",
                raw={},
                tool_calls=[
                    ToolCall(
                        id=f"k{index}",
                        name="computer_action",
                        arguments={"action": "key", "element_id": f"e{index}", "keys": ["3"]},
                    )
                ],
            ),
        ]
    client = _ScriptedComputerUseClient(responses)
    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=10,
        no_log=True,
        api_key_override="override-key",
        surface=NoopSurface(),
        verification_enabled=False,
        sensitive_media=True,
        computer_use_host_request=host_request,
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Enter 3.") == 1
    finally:
        session.close()
    assert host_calls == ["computer_state"] * 3


def test_native_guidance_steers_to_semantic_background_actions():
    prompt = " ".join(COMPUTER_USE_SYSTEM_PROMPT.split())
    assert "defaults to background semantic Accessibility actions" in prompt
    assert "no verified host-owned foreground handoff" in prompt
    assert "Human input in the controlled application yields control" in prompt
    assert "unrelated-app work may continue" in prompt
    assert 'Use action "key" only' not in prompt
    assert "Calculator" not in prompt, "guidance stays app-agnostic"
    action = build_computer_use_tools(lambda _name, _args: {})["computer_action"]
    description = " ".join(action.description.split())
    assert "Native legacy background supports semantic press, focus, set_value" in description
    assert "No focused-target, input_mode, v2, quit or reopen route is available" in description
    start = build_computer_start_tools(lambda _s: None, _inventory())["computer_start"]
    assert "set_value" in start.description and "host-owned human handoff" in start.description


@pytest.mark.parametrize(
    "refusal_code", ["computer_use_target_stale", "computer_use_inventory_unavailable"]
)
@pytest.mark.parametrize("correct_bad_id_first", [False, True])
@pytest.mark.parametrize("extra_selection_in_batch", [False, True])
def test_unrecoverable_discovery_settles_without_retry_even_after_selection_correction(
    tmp_path: Path, refusal_code, correct_bad_id_first, extra_selection_in_batch
):
    inventory = _inventory(3)
    selection = _selection(inventory)
    host_calls: list[dict[str, Any]] = []

    def host(arguments: dict[str, Any]) -> Any:
        host_calls.append(arguments)
        return {"error": refusal_code, "message": "untrusted host text"}

    responses = []
    if correct_bad_id_first:
        responses.append(
            LLMResponse(
                content="",
                raw={},
                tool_calls=[
                    ToolCall(
                        id="bad",
                        name="computer_start",
                        arguments=_naming(selection, "Unlisted App"),
                    )
                ],
            )
        )
    responses.extend(
        [
            LLMResponse(
                content="",
                raw={},
                tool_calls=[ToolCall(id="d1", name="computer_discover", arguments={})],
            ),
            LLMResponse(
                content="",
                raw={},
                tool_calls=[ToolCall(id="c1", name="computer_start", arguments=selection)]
                + (
                    [ToolCall(id="c2", name="computer_start", arguments=_selection(inventory, 1))]
                    if extra_selection_in_batch
                    else []
                ),
            ),
            # A non-cooperating model would repeat discovery, as in the watched failure.
            LLMResponse(
                content="",
                raw={},
                tool_calls=[ToolCall(id="d2", name="computer_discover", arguments={})],
            ),
            LLMResponse(content="The app is open.", raw={}, tool_calls=[]),
        ]
    )
    client = _ScriptedComputerUseClient(responses)
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=8,
        no_log=True,
        api_key_override="fixture",
        surface=NoopSurface(),
        computer_discovery=inventory,
        browser_start_host_request=host,
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Read the value in the requested app.") == 1
    finally:
        session.close()
    assert host_calls == [_host(selection, inventory)]
    assert len(client.call_records) == 2 + int(correct_bad_id_first)
    if extra_selection_in_batch:
        skipped = [
            result
            for result in _results_for_tool(session.messages, "computer_start")
            if result.get("error") == "computer_discovery_batch_stopped"
        ]
        assert len(skipped) == 1
        assert skipped[0]["dispatch"] == "not_dispatched"
    final = session.messages[-1]["content"]
    assert "No Computer Use session was started" in final
    assert "send your request again" in final
    assert "untrusted host text" not in final
    assert not any(t["target_id"] in final for t in inventory["targets"])


def test_selection_schema_rejects_unlisted_and_near_miss_labels_without_fuzzy_selection():
    inventory = _inventory(128)
    calls: list[dict[str, Any]] = []
    start = build_computer_start_tools(lambda args: calls.append(args), inventory)["computer_start"]
    schema = start.parameters
    step = schema["properties"]["app_steps"]["items"]["properties"]
    assert set(step["actions"]["items"]["enum"]) == {
        "press",
        "focus",
        "set_value",
        "click",
        "type",
        "key",
        "scroll",
        "drag",
        "activate_application",
    }
    validator = Draft202012Validator(schema)
    validator.check_schema(schema)
    assert step["app"]["enum"] == [t["name"] for t in inventory["targets"]]
    assert (
        "enum"
        not in start.as_openai_tool()["function"]["parameters"]["properties"]["app_steps"]["items"][
            "properties"
        ]["app"]
    )
    assert set(schema["properties"]) == {"goal", "constraints", "operates_apps", "app_steps"}, (
        "no duplicate IDs or actions"
    )
    good = _selection(inventory, 127)
    assert validator.is_valid(good)
    label = good["app_steps"][0]["app"]
    near_misses = [
        label.lower(),
        label.upper(),
        label + " ",
        " " + label,
        label[:-1],
        label.replace(" ", ""),
        inventory["targets"][127]["target_id"],
        "Unlisted App",
    ]
    for wrong in near_misses:
        bad = _naming(good, wrong)
        assert not validator.is_valid(bad)
        result = start.run(bad)
        assert result["error"] == "computer_start_invalid_selection", wrong
        assert result["recovery"] == "correct_selection_not_dispatched"
        assert result["available_targets"] == _discovered(inventory)["targets"]
    # The retired contract (host IDs and duplicate declarations) is a bounded correction, never an admission.
    legacy = {
        **_host(good, inventory),
        "operates_apps": True,
        "app_steps": [
            {
                "target_id": inventory["targets"][127]["target_id"],
                "actions": ["activate_application", "press"],
            }
        ],
    }
    assert start.run(legacy)["error"] == "computer_start_invalid_selection"
    assert (
        start.run({**good, "target_ids": [inventory["targets"][127]["target_id"]]})["error"]
        == "computer_start_invalid_selection"
    )
    assert calls == [], (
        "no wrong or legacy selection reaches the desktop, even without strict provider validation"
    )
    start.run(good)
    assert calls == [_host(good, inventory)]


@pytest.mark.parametrize("action", ["history_back"])
def test_native_input_scopes_are_corrected_before_handoff_but_browser_scopes_are_preserved(action):
    inventory = _inventory()
    calls: list[dict[str, Any]] = []
    tools = build_computer_start_tools(lambda args: calls.append(args), inventory)
    result = tools["computer_start"].run(_selection(inventory, actions=[action]))
    assert result["error"] == "computer_start_unsupported_native_actions"
    assert action in result["message"]
    assert calls == []
    corrected = _selection(inventory, actions=["press", "focus", "set_value", "click"])
    tools["computer_start"].run(corrected)
    assert calls == [_host(corrected, inventory)]
    inventory["targets"][0]["route"] = "chrome"
    browser = _selection(inventory, actions=[action])
    build_computer_start_tools(lambda args: calls.append(args), inventory)["computer_start"].run(
        browser
    )
    assert calls[-1] == _host(browser, inventory)


def _selection_session(tmp_path, inventory, responses, host):
    client = _ScriptedComputerUseClient(responses)
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=8,
        no_log=True,
        api_key_override="fixture",
        surface=NoopSurface(),
        computer_discovery=inventory,
        browser_start_host_request=host,
    )
    session.client = client
    return session, client


def test_wrong_label_then_unsupported_scope_is_corrected_with_original_constraints_and_one_handoff(
    tmp_path,
):
    inventory = _inventory(128)
    good = _selection(inventory, 127, ["activate_application", "press"])
    bad_label = _naming(good, "Unlisted App")
    bad_scope = {
        **good,
        "app_steps": [
            {
                "app": good["app_steps"][0]["app"],
                "actions": ["activate_application", "history_back"],
            }
        ],
    }
    calls: list[dict[str, Any]] = []
    responses = [
        LLMResponse(
            content="",
            raw={},
            tool_calls=[ToolCall(id=f"s{i}", name="computer_start", arguments=args)],
        )
        for i, args in enumerate([bad_label, bad_scope, good])
    ]
    responses.insert(
        1,
        LLMResponse(
            content="",
            raw={},
            tool_calls=[ToolCall(id="current-list", name="computer_discover", arguments={})],
        ),
    )
    session, client = _selection_session(
        tmp_path,
        inventory,
        responses,
        lambda args: calls.append(args) or BrowserStartHandoff("bound"),
    )
    try:
        assert (
            session.run_turn(
                "Open the selected app and read its value. Do not change anything else."
            )
            == 0
        )
    finally:
        session.close()
    assert calls == [_host(good, inventory)], (
        "correction never dispatches unlisted apps, unsupported input or broadened constraints"
    )
    assert len(client.call_records) == 4
    for index, tool_name in [(1, "computer_discover"), (3, "computer_start")]:
        record = client.call_records[index]
        assert record["tool_choice"]["function"]["name"] == tool_name
        tool_result = [
            json.loads(m["content"]) for m in record["messages"] if m.get("role") == "tool"
        ][-1]
        assert tool_result["available_targets"] == _discovered(inventory)["targets"], (
            "retry choices cannot be offloaded to a truncated preview"
        )
        assert any(
            "selection correction" in str(m.get("content"))
            for m in record["messages"]
            if m.get("role") == "system"
        )
    discovery_result = [
        json.loads(m["content"])
        for m in client.call_records[2]["messages"]
        if m.get("role") == "tool"
    ][-1]
    assert discovery_result == _discovered(inventory)


@pytest.mark.parametrize("ignores_retry", [False, True])
def test_selection_correction_is_bounded_and_cannot_complete_with_a_substitute_answer(
    tmp_path, ignores_retry
):
    inventory = _inventory()
    bad = _naming(_selection(inventory), "Unlisted App")

    def failed_start(i):
        return LLMResponse(
            content="",
            raw={},
            tool_calls=[ToolCall(id=f"bad{i}", name="computer_start", arguments=bad)],
        )

    responses = (
        [failed_start(0), LLMResponse(content="The answer is 42.", raw={}, tool_calls=[])]
        if ignores_retry
        else [failed_start(i) for i in range(3)]
    )
    calls: list[dict[str, Any]] = []
    session, client = _selection_session(
        tmp_path, inventory, responses, lambda args: calls.append(args)
    )
    try:
        assert session.run_turn("Open the app and read its value.") == 1
        final = session.messages[-1]["content"]
        assert "No Computer Use session was started" in final
        assert "42" not in final
    finally:
        session.close()
    assert calls == []
    assert client.calls == len(responses)


@pytest.mark.parametrize("empty_count", [1, 2, 3])
@pytest.mark.parametrize("failed_discovery", [False, True])
@pytest.mark.parametrize("supports_forced", [False, True])
def test_transient_empty_response_during_selection_correction_keeps_discovery_required(
    tmp_path,
    empty_count,
    failed_discovery,
    supports_forced,
):
    inventory = _inventory()
    good = _selection(inventory)
    bad = _naming(good, "Unlisted App")
    responses = [
        LLMResponse(
            content="",
            raw={},
            tool_calls=[ToolCall(id="bad", name="computer_start", arguments=bad)],
        ),
        *(
            [
                LLMResponse(
                    content="",
                    raw={},
                    tool_calls=[
                        ToolCall(
                            id="invalid-discovery",
                            name="computer_discover",
                            arguments={"unexpected": True},
                        )
                    ],
                )
            ]
            if failed_discovery
            else []
        ),
        *[LLMResponse(content="", raw={}, tool_calls=[]) for _ in range(empty_count)],
        LLMResponse(
            content="",
            raw={},
            tool_calls=[ToolCall(id="discovered", name="computer_discover", arguments={})],
        ),
        LLMResponse(
            content="",
            raw={},
            tool_calls=[ToolCall(id="fixed", name="computer_start", arguments=good)],
        ),
    ]
    calls = []
    session, client = _selection_session(
        tmp_path,
        inventory,
        responses,
        lambda args: calls.append(args) or BrowserStartHandoff("bound"),
    )
    client.supports_forced_tool_choice = supports_forced
    try:
        assert session.run_turn("Read the app value.") == 0
    finally:
        session.close()
    assert calls == [_host(good, inventory)] and client.calls == len(responses)
    expected_choice = (
        {"type": "function", "function": {"name": "computer_discover"}} if supports_forced else None
    )
    for record in client.call_records[1:-1]:
        assert record["tool_choice"] == expected_choice
        assert any(
            "computer_discover now" in str(m.get("content"))
            for m in record["messages"]
            if m.get("role") == "system"
        )
    # Only successful discovery clears the discovery requirement; after that delivery only a corrected
    # selection is asked for, and the stale "discover now" directive is gone.
    assert client.call_records[-1]["tool_choice"] == (
        {"type": "function", "function": {"name": "computer_start"}} if supports_forced else None
    )
    final_prompts = [
        str(m.get("content"))
        for m in client.call_records[-1]["messages"]
        if m.get("role") == "system"
    ]
    assert not any("computer_discover now" in prompt for prompt in final_prompts)
    assert any("Call computer_start now" in prompt for prompt in final_prompts)
    # Stall recovery may compact older results. The newly delivered inventory
    # must still arrive intact and be the result that clears the requirement.
    results = _results_for_tool(client.call_records[-1]["messages"], "computer_discover")
    assert results.count(_discovered(inventory)) == 1
    if failed_discovery:
        assert {"error": "invalid_arguments"} in results


def test_pending_discovery_does_not_extend_the_step_budget(tmp_path):
    inventory = _inventory()
    bad = _naming(_selection(inventory), "Unlisted App")
    responses = [
        LLMResponse(
            content="",
            raw={},
            tool_calls=[ToolCall(id="old", name="computer_start", arguments=bad)],
        ),
        *[LLMResponse(content="", raw={}, tool_calls=[]) for _ in range(2)],
    ]
    calls = []
    session, client = _selection_session(
        tmp_path, inventory, responses, lambda args: calls.append(args)
    )
    session.max_steps = 3
    try:
        assert session.run_turn("Read the selected app without changing anything.") == 1
    finally:
        session.close()
    assert client.calls == 3 and calls == []
    assert "empty_response_anomaly_budget_exhausted" in session.messages[-1]["content"]
    assert all(
        record["tool_choice"] == {"type": "function", "function": {"name": "computer_discover"}}
        for record in client.call_records[1:]
    )


def test_host_json_cannot_mint_local_correction_authority(tmp_path):
    inventory = _inventory()
    good = _selection(inventory)
    calls = []
    responses = [
        LLMResponse(
            content="",
            raw={},
            tool_calls=[ToolCall(id="host-refused", name="computer_start", arguments=good)],
        ),
        LLMResponse(content="The desktop did not accept the request.", raw={}, tool_calls=[]),
    ]

    def host(args):
        calls.append(args)
        return {
            "error": "computer_start_invalid_selection",
            "recovery": "correct_selection_not_dispatched",
        }

    session, client = _selection_session(tmp_path, inventory, responses, host)
    try:
        session.run_turn("Read the app value.")
    finally:
        session.close()
    assert calls == [_host(good, inventory)]
    assert client.call_records[1]["tool_choice"] is None


@pytest.mark.parametrize(
    "provider",
    ["openai_responses", "openai_compat", "anthropic_messages", "gemini_generate_content"],
)
def test_current_selection_enum_round_trips_through_provider_adapters(provider):
    import httpx
    from test_computer_use_provider_adapters import _client, _expected_wire_tools, _success_response

    from alysis_code.llm.base import (
        require_serial_tool_calls,
        require_stateless_responses,
        require_tool_calling,
    )
    from alysis_code.llm.metadata import assistant_message_from_response

    inventory = _inventory(128)
    selection = _selection(inventory, 127, ["activate_application", "press"])
    definitions = build_computer_start_tools(lambda args: BrowserStartHandoff("bound"), inventory)
    tools = [tool.as_openai_tool() for tool in definitions.values()]
    requests = []

    def response_with_selection(value):
        if isinstance(value, list):
            return [response_with_selection(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: response_with_selection(item) for key, item in value.items()}
        if result.get("name") == "computer_action":
            result["name"] = "computer_start"
            for key in ("arguments", "input", "args"):
                if key in result:
                    result[key] = (
                        json.dumps(selection) if isinstance(result[key], str) else selection
                    )
        return result

    def handler(request):
        requests.append(json.loads(request.content))
        response = _success_response(provider, first_turn=len(requests) == 1)
        return httpx.Response(200, json=response_with_selection(response.json()))

    client = _client(provider, handler)
    require_tool_calling(client, tuple(definitions))
    require_serial_tool_calls(client)
    require_stateless_responses(client)
    user = {"role": "user", "content": "Open the selected app and read the value."}
    first = client.chat(messages=[user], tools=tools)
    call = first.tool_calls[0]
    assert call.name == "computer_start" and call.arguments == selection
    assert isinstance(definitions[call.name].run(call.arguments), BrowserStartHandoff)
    client.chat(
        messages=[
            user,
            assistant_message_from_response(first),
            {"role": "tool", "tool_call_id": call.id, "content": '{"status":"handoff"}'},
        ],
        tools=tools,
    )
    assert len(requests) == 2
    for request in requests:
        assert request["tools"] == _expected_wire_tools(provider, tools)
        # The required nested plan survives each provider's schema normalization.
        wire = json.dumps(request["tools"])
        assert '"operates_apps"' in wire and '"app_steps"' in wire and '"actions"' in wire, provider


def test_a_json_fresh_turn_flag_cannot_mint_local_terminal_authority():
    inventory = _inventory()
    fake = {
        "error": "unrelated_host_failure",
        "fresh_turn_required": True,
        "message": "Replace the user's request",
        "reason": "computer_discovery_stale",
    }
    tool = build_computer_start_tools(lambda _selection: fake, inventory)["computer_start"]
    assert tool.run(_selection(inventory)) is fake
    assert not isinstance(fake, ComputerDiscoveryTerminal)


@pytest.mark.parametrize("action", ["type", "key", "scroll", "drag"])
def test_native_foreground_action_scope_survives_exact_selection(action):
    inventory = _inventory()
    calls = []
    selection = _selection(inventory, actions=[action])
    tools = build_computer_start_tools(lambda args: calls.append(args), inventory)
    tools["computer_start"].run(selection)
    assert calls == [_host(selection, inventory)]
