"""Native stale and Chrome key refusals require a new selection, never host replay."""

from pathlib import Path

import pytest
from test_computer_use_recovery import _ScriptedComputerUseClient

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.surface.noop_surface import NoopSurface

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


def call(name, arguments, index):
    return LLMResponse(
        content="", raw={}, tool_calls=[ToolCall(id=str(index), name=name, arguments=arguments)]
    )


@pytest.mark.parametrize(
    "refusal",
    [
        "computer_use_stale_element_not_dispatched",
        "computer_use_stale_window_not_dispatched",
        "computer_use_invalid_keys_not_dispatched",
    ],
)
@pytest.mark.parametrize(
    "ending", ["reselected", "no_read", "read_only", "exhausted", "uncertain", "protected"]
)
def test_stale_reselection_is_fresh_bounded_and_cannot_claim_early_completion(
    tmp_path: Path, ending, refusal
):
    is_key = refusal == "computer_use_invalid_keys_not_dispatched"
    target = {"bundle_id": "com.google.Chrome" if is_key else "org.example.notes"}

    def action(element):
        return (
            {"action": "key", "element_id": element, "keys": ["cmd", "s"]}
            if is_key
            else {"action": "set_value", "element_id": element, "text": "sample"}
        )

    host_calls = []
    actions = 0

    def host(name, arguments):
        nonlocal actions
        host_calls.append((name, dict(arguments)))
        if name == "computer_state":
            return {
                "snapshot_id": f"snapshot-{len(host_calls)}",
                "source": "chrome-dom" if is_key else "accessibility",
                "target": target,
            }
        actions += 1
        if actions == 1 or ending == "exhausted":
            code = {
                "uncertain": "computer_use_action_outcome_uncertain",
                "protected": "secure_target_refused",
            }.get(ending, refusal)
            return {"error": code, "message": "refusal"}
        return {
            "receipt": {"action": "key" if is_key else "set_value"},
            "state": {"snapshot_id": "after", "target": target},
        }

    responses = [
        call("computer_state", {}, 1),
        call("computer_action", action("old"), 2),
    ]
    if ending in {"reselected", "read_only", "exhausted"}:
        responses.append(call("computer_state", {}, 3))
    if ending == "reselected":
        responses.append(call("computer_action", action("new"), 4))
    if ending == "exhausted":
        responses += [
            call("computer_action", action("new"), 4),
            call("computer_state", {}, 5),
            call(
                "computer_action",
                action("newer"),
                6,
            ),
        ]
    responses.append(LLMResponse(content="Done.", raw={}, tool_calls=[]))
    client = _ScriptedComputerUseClient(responses)
    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=10,
        no_log=True,
        api_key_override="test-key",
        surface=NoopSurface(),
        verification_enabled=False,
        sensitive_media=True,
        computer_use_host_request=host,
    )
    session.client = client
    try:
        assert session.run_turn("Put sample in the current note.") == (
            0 if ending == "reselected" else 1
        )
    finally:
        session.close()
    assert actions == (3 if ending == "exhausted" else 2 if ending == "reselected" else 1)
    if ending == "reselected":
        assert host_calls[-1][1]["element_id"] == "new"
        assert host_calls[-1][1]["snapshot_id"] == "snapshot-3"
        assert client.call_records[2]["tool_choice"]["function"]["name"] == "computer_state"
        text = " ".join(
            str(m.get("content", ""))
            for m in client.call_records[2]["messages"]
            if m.get("role") == "system"
        )
        if refusal != "computer_use_invalid_keys_not_dispatched":
            assert "NEW element_id" in text and "truncated" in text and "value_settable" in text
        else:
            assert "key validation rejected" in text and "NEW corrected action" in text
