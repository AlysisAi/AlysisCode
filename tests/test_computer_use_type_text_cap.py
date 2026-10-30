from __future__ import annotations

from copy import deepcopy
from typing import Any

import jsonschema
import pytest
from test_computer_interaction_v2 import action_result, call, done, run_v2_turn, state
from test_ide_computer_use_host import _lab_state

from alysis_code.agent.computer_use_tools import (
    COMPUTER_USE_SYSTEM_PROMPT,
    COMPUTER_USE_TYPE_TEXT_MAX_UTF16_UNITS,
    build_computer_use_tools,
)

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

ASTRAL = chr(0x1F600)  # one character, two UTF-16 code units


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _legacy_type(text: str) -> dict[str, Any]:
    return {"action": "type", "element_id": "element-text", "text": text}


def _v2_type(text: str) -> dict[str, Any]:
    return {
        "action": "type",
        "target_type": "focused_input",
        "input_mode": "background",
        "recipient_ref": "recipient-1",
        "text": text,
    }


def _recording_host(calls: list[tuple[str, dict[str, Any]]], *, v2: bool):
    def host(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, deepcopy(arguments)))
        if not v2:
            return _lab_state(f"snapshot-{len(calls)}", "READY")
        if name == "computer_action":
            return action_result(arguments, state(2))
        return state(1)

    return host


@pytest.mark.parametrize("interaction_v2", [False, True])
def test_type_schema_caps_text_at_5000_while_set_value_keeps_10000(interaction_v2: bool) -> None:
    tools = build_computer_use_tools(
        lambda _name, _arguments: {}, observation_available=True, interaction_v2=interaction_v2
    )
    schema = tools["computer_action"].parameters
    assert COMPUTER_USE_TYPE_TEXT_MAX_UTF16_UNITS == 5_000
    if interaction_v2:
        type_call = _v2_type
        set_value = {"action": "set_value", "target_type": "semantic", "element_id": "element-text"}
    else:
        type_call = _legacy_type
        set_value = {"action": "set_value", "element_id": "element-text"}

    jsonschema.validate(type_call("a" * 5_000), schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(type_call("a" * 5_001), schema)
    jsonschema.validate({**set_value, "text": "a" * 10_000}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**set_value, "text": "a" * 10_001}, schema)
    assert "5,000 UTF-16 code units" in schema["properties"]["text"]["description"]


@pytest.mark.parametrize("interaction_v2", [False, True])
def test_type_text_over_5000_utf16_units_is_refused_before_any_host_request(
    interaction_v2: bool,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    tools = build_computer_use_tools(
        _recording_host(calls, v2=interaction_v2),
        observation_available=True,
        interaction_v2=interaction_v2,
    )
    tools["computer_state"].run({})
    # 2,501 characters satisfy the schema's maxLength but are 5,002 UTF-16 units.
    text = ASTRAL * 2_501
    assert len(text) <= COMPUTER_USE_TYPE_TEXT_MAX_UTF16_UNITS < _utf16(text)
    jsonschema.validate(
        (_v2_type if interaction_v2 else _legacy_type)(text), tools["computer_action"].parameters
    )

    result = tools["computer_action"].run((_v2_type if interaction_v2 else _legacy_type)(text))

    assert result["error"] == "invalid_text"
    assert result["interaction_feedback"]["dispatch"] == "not_dispatched"
    assert "Nothing was typed" in result["message"]
    assert text not in repr(result)
    assert [name for name, _ in calls] == ["computer_state"]
    # The refusal also retires the snapshot: the next action needs a fresh observation.
    follow_up = tools["computer_action"].run((_v2_type if interaction_v2 else _legacy_type)("ok"))
    assert follow_up["error"] == "computer_use_fresh_observation_required"
    assert [name for name, _ in calls] == ["computer_state"]


def test_type_text_of_exactly_5000_utf16_units_is_sent_unchanged() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    tools = build_computer_use_tools(
        _recording_host(calls, v2=True), observation_available=True, interaction_v2=True
    )
    tools["computer_state"].run({})
    # Combining marks, astral characters and a newline stay exactly as given.
    text = ASTRAL * 2_498 + "a" + chr(0x301) + "\n"
    assert _utf16(text) == COMPUTER_USE_TYPE_TEXT_MAX_UTF16_UNITS - 1
    text += "z"

    result = tools["computer_action"].run(_v2_type(text))

    assert "error" not in result
    assert calls[-1][0] == "computer_action"
    assert calls[-1][1]["text"] == text


def test_set_value_text_keeps_its_10000_unit_contract() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    tools = build_computer_use_tools(
        _recording_host(calls, v2=False), observation_available=True, interaction_v2=False
    )
    tools["computer_state"].run({})
    text = "a" * 6_000

    tools["computer_action"].run(
        {"action": "set_value", "element_id": "element-text", "text": text}
    )

    assert calls[-1][0] == "computer_action"
    assert calls[-1][1]["text"] == text


def test_guidance_plans_smaller_segments_and_observes_without_replay_or_truncation() -> None:
    prompt = " ".join(COMPUTER_USE_SYSTEM_PROMPT.split())
    for expected in (
        "One type action carries at most 5,000 UTF-16 code units",
        "plan the exact intended text as smaller segments",
        "observe after each that it arrived before sending the next",
        "Never truncate or rephrase the text",
        "Never resend a segment whose delivery was uncertain or partial",
        "If no safe segment can be formed, stop and report it",
    ):
        assert expected in prompt


def test_oversized_type_in_a_v2_turn_replans_without_replay(tmp_path) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []

    def host(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, deepcopy(arguments)))
        if name == "computer_action":
            return action_result(arguments, state(3), effect="observed_change")
        return state(len(host_calls))

    oversized = ASTRAL * 2_600
    segment = ASTRAL * 2_500
    code, surface, _client = run_v2_turn(
        tmp_path,
        host,
        [
            call("computer_state", {}, 1),
            call("computer_action", {**_v2_type(oversized), "recipient_ref": "recipient-1"}, 2),
            call("computer_state", {}, 3),
            call("computer_action", {**_v2_type(segment), "recipient_ref": "recipient-2"}, 4),
            done(),
        ],
    )

    actions = [arguments for name, arguments in host_calls if name == "computer_action"]
    # The oversized text never reached the host, and nothing was split or resent for it.
    assert [arguments["text"] for arguments in actions] == [segment]
    assert [name for name, _ in host_calls] == [
        "computer_state",
        "computer_state",
        "computer_action",
    ]
    assert code == 0, surface.final_messages
