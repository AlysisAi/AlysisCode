from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_computer_use_recovery import _ScriptedComputerUseClient

from alysis_code.agent.computer_use_tools import build_computer_use_tools
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.surface.noop_surface import NoopSurface

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

# The controller (06cc8ae) refuses a Chrome multi-key list locally, before any host action,
# with the existing recoverable code; the engine must reuse its bounded fresh observation.
REFUSAL = {
    "error": "computer_use_invalid_keys_not_dispatched",
    "message": "Chrome accepts one key, optionally with modifiers, per action.",
}


def _chrome_state(index: int) -> dict[str, Any]:
    return {"snapshot_id": f"chrome-state-{index}", "source": "chrome-dom"}


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        content="", tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)], raw={}
    )


def _run(
    tmp_path: Path, responses: list[LLMResponse], host_request: Any, max_steps: int = 8
) -> tuple[int, _ScriptedComputerUseClient]:
    client = _ScriptedComputerUseClient(responses)
    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=max_steps,
        no_log=True,
        api_key_override="override-key",
        surface=NoopSurface(),
        verification_enabled=False,
        sensitive_media=True,
        computer_use_host_request=host_request,
    )
    session.client = client  # type: ignore[assignment]
    try:
        return session.run_turn("Move to the next field on this page."), client
    finally:
        session.close()


def test_key_guidance_states_one_key_per_chrome_action_and_keeps_native_sequences() -> None:
    tools = build_computer_use_tools(lambda _name, _arguments: {})
    description = tools["computer_action"].parameters["properties"]["keys"]["description"]
    assert (
        "Chrome pages (source chrome-dom) accept exactly one key, optionally with modifiers, per action"
        in description
    )
    assert (
        "send each key as its own action and check the returned state before the next"
        in description
    )
    assert "ordered 1-64 key sequence" in description
    assert "exactly one non-modifier key" in description
    assert "meta/command/super=cmd" in description


def test_chrome_multi_key_refusal_reobserves_then_sends_one_corrected_key(tmp_path: Path) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, dict(arguments)))
        if name == "computer_state":
            return _chrome_state(len(host_calls))
        if len(arguments.get("keys") or []) != 1:
            return dict(REFUSAL)
        return {
            "ok": True,
            "receipt": {"action": "key", "source": "chrome-dom"},
            "state": _chrome_state(len(host_calls)),
        }

    code, client = _run(
        tmp_path,
        [
            _call("state-1", "computer_state", {}),
            _call(
                "keys-1",
                "computer_action",
                {"action": "key", "element_id": "field-1", "keys": ["tab", "tab"]},
            ),
            _call("state-2", "computer_state", {}),
            _call(
                "key-1",
                "computer_action",
                {"action": "key", "element_id": "field-1", "keys": ["tab"]},
            ),
            LLMResponse(
                content="Moved focus with one Tab and observed the page.", tool_calls=[], raw={}
            ),
        ],
        host_request,
    )

    assert code == 0
    assert [name for name, _arguments in host_calls] == [
        "computer_state",
        "computer_action",
        "computer_state",
        "computer_action",
    ]
    # The refused list was sent once by the model and never resent by the engine.
    assert [arguments["keys"] for name, arguments in host_calls if name == "computer_action"] == [
        ["tab", "tab"],
        ["tab"],
    ]
    recovery = client.call_records[2]
    assert recovery["tool_choice"]["function"]["name"] == "computer_state"
    assert any(
        "key validation rejected" in str(message.get("content") or "")
        for message in recovery["messages"]
        if message.get("role") == "system"
    )


def test_chrome_key_refusal_recovery_is_bounded_and_never_replays(tmp_path: Path) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, dict(arguments)))
        return _chrome_state(len(host_calls)) if name == "computer_state" else dict(REFUSAL)

    responses: list[LLMResponse] = []
    for index in range(3):
        responses += [
            _call(f"state-{index}", "computer_state", {}),
            _call(
                f"keys-{index}",
                "computer_action",
                {"action": "key", "element_id": f"field-{index}", "keys": ["a", "b"]},
            ),
        ]
    code, client = _run(tmp_path, responses, host_request, max_steps=10)

    assert code == 1
    actions = [arguments for name, arguments in host_calls if name == "computer_action"]
    assert len(actions) == 3 and all(arguments["keys"] == ["a", "b"] for arguments in actions)
    assert client.calls == 6
