from __future__ import annotations

from itertools import count
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.computer_use_tools import build_computer_use_tools
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.surface.noop_surface import NoopSurface

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


class _ScriptedComputerUseClient:
    model = "test-model"
    temperature = 0.2
    supports_forced_tool_choice = True

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.calls = 0
        self.call_records: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        stream: bool = False,
        on_text_delta: Any | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = stream, on_text_delta, temperature
        self.call_records.append(
            {
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
            }
        )
        response = self.responses[self.calls]
        self.calls += 1
        return response


def _undispatched_stale_action() -> dict[str, Any]:
    return {
        "receipt": {
            "action": "press",
            "performed": False,
            "status": "not_dispatched",
            "reason": "stale_snapshot",
            "source": "chrome-dom",
        },
        "state": {"snapshot_id": "replacement-state", "source": "chrome-dom"},
    }


def test_exact_stale_refusal_requires_a_new_read_instead_of_adopting_replacement_binding() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def host(name, arguments):
        calls.append((name, dict(arguments)))
        if name == "computer_state":
            return {"snapshot_id": f"fresh-{len(calls)}", "source": "chrome-dom"}
        return (
            _undispatched_stale_action()
            if len(calls) == 2
            else {
                "receipt": {"action": "press", "performed": True},
                "state": {"snapshot_id": "after"},
            }
        )

    tools = build_computer_use_tools(host)
    tools["computer_state"].run({})
    action = {"action": "press", "element_id": "link"}
    assert (
        tools["computer_action"].run(action)["error"] == "computer_use_fresh_observation_required"
    )
    assert (
        tools["computer_action"].run(action)["error"] == "computer_use_fresh_observation_required"
    )
    assert len(calls) == 2, "neither the stale action nor an early retry is replayed"
    tools["computer_state"].run({})
    assert tools["computer_action"].run(action)["state"]["snapshot_id"] == "after"
    assert calls[-1][1]["snapshot_id"] == "fresh-3"


@pytest.mark.parametrize(
    "patch",
    [
        {"performed": 0},
        {"performed": True},
        {"status": "uncertain"},
        {"reason": "permission_required"},
        {"source": "chrome-tabs"},
        {"action": "type"},
    ],
)
def test_nonmatching_action_receipts_cannot_enter_stale_snapshot_recovery(patch) -> None:
    result = _undispatched_stale_action()
    result["receipt"].update(patch)
    tools = build_computer_use_tools(
        lambda name, _: {"snapshot_id": "before"} if name == "computer_state" else result
    )
    tools["computer_state"].run({})
    assert tools["computer_action"].run({"action": "press", "element_id": "link"}) == result


@pytest.mark.parametrize(
    "variant,expected",
    [
        ("workflow", "invalid_host_workflow"),
        ("state", "invalid_host_response"),
        ("permission", "computer_use_approval_denied"),
        ("uncertain", "computer_use_action_outcome_uncertain"),
    ],
)
def test_stale_receipt_cannot_mask_invalid_host_state_workflow_permission_or_uncertainty(
    variant, expected
) -> None:
    result = _undispatched_stale_action()
    if variant == "workflow":
        result["alysis_workflow"] = {"version": 999}
    elif variant == "state":
        result["state"]["snapshot_id"] = None
    else:
        result["error"] = expected
    tools = build_computer_use_tools(
        lambda name, _: {"snapshot_id": "before"} if name == "computer_state" else result
    )
    tools["computer_state"].run({})
    assert (
        tools["computer_action"].run({"action": "press", "element_id": "link"})["error"] == expected
    )


@pytest.mark.parametrize(
    "refusal", ["computer_use_key_target_not_routable", "computer_use_invalid_keys_not_dispatched"]
)
def test_local_native_key_refusal_precedes_host_feedback_then_allows_safe_press(
    tmp_path: Path, refusal: str
) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, dict(arguments)))
        if name == "computer_state":
            return {
                "snapshot_id": f"state-{len(host_calls)}",
                "target": {"bundle_id": "com.apple.calculator"},
            }
        if arguments.get("action") == "key":
            bridge_error = {
                "error": refusal,
                "message": "The native key target is not keyboard-routable.",
            }
            assert bridge_error == {
                "error": refusal,
                "message": "The native key target is not keyboard-routable.",
            }
            return bridge_error
        return {
            "ok": True,
            "receipt": {"action": "press"},
            "state": {
                "snapshot_id": f"state-{len(host_calls)}",
                "target": {"bundle_id": "com.apple.calculator"},
            },
        }

    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="state-1", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="key-1",
                        name="computer_action",
                        arguments={
                            "action": "key",
                            "element_id": "calculator-button",
                            "keys": ["3", "9", "8", "4"],
                        },
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="state-2", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="press-1",
                        name="computer_action",
                        arguments={
                            "action": "press",
                            "element_id": "calculator-3",
                        },
                    )
                ],
                raw={},
            ),
            LLMResponse(content="safe recovery complete", tool_calls=[], raw={}),
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
        assert session.run_turn("Use Calculator to enter 3984.") == 0
    finally:
        session.close()

    assert [name for name, _arguments in host_calls] == [
        "computer_state",
        "computer_state",
        "computer_action",
    ]
    assert host_calls[-1][1]["action"] == "press"
    assert all(arguments.get("action") != "key" for _name, arguments in host_calls[2:])
    recovery_messages = client.call_records[2]["messages"]
    assert any(
        "unavailable for the current observation" in str(message.get("content") or "")
        for message in recovery_messages
        if message.get("role") == "system"
    )
    assert client.call_records[2]["tool_choice"]["function"]["name"] == "computer_state"


@pytest.mark.parametrize(
    "refusal", ["computer_use_key_target_not_routable", "computer_use_invalid_keys_not_dispatched"]
)
def test_local_native_key_refusals_are_bounded_before_host_feedback(
    tmp_path: Path, refusal: str
) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, dict(arguments)))
        if name == "computer_state":
            return {"snapshot_id": f"state-{len(host_calls)}"}
        return {
            "error": refusal,
            "message": "The native key target is not keyboard-routable.",
        }

    responses: list[LLMResponse] = []
    for index in range(3):
        responses.extend(
            [
                LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id=f"state-{index}", name="computer_state", arguments={})],
                    raw={},
                ),
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id=f"key-{index}",
                            name="computer_action",
                            arguments={
                                "action": "key",
                                "element_id": f"wrong-target-{index}",
                                "keys": ["3"],
                            },
                        )
                    ],
                    raw={},
                ),
            ]
        )
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
        assert session.run_turn("Use Calculator to enter 3.") == 1
    finally:
        session.close()

    assert [name for name, _arguments in host_calls] == [
        "computer_state",
        "computer_state",
        "computer_state",
    ]
    assert client.calls == 6


_call_ids = count()


def _response(name: str, **arguments: Any) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(id=f"call-{next(_call_ids)}", name=name, arguments=arguments)],
        raw={},
    )


def _run_page_scenario(
    tmp_path: Path, responses: list[LLMResponse], host_request: Any, *, max_steps: int = 10
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
        sensitive_media=True,
        computer_use_host_request=host_request,
    )
    session.client = client  # type: ignore[assignment]
    try:
        return session.run_turn("Read all entries in the selected document."), client
    finally:
        session.close()


def test_stale_page_recovery_restarts_the_host_read_even_if_model_repeats_cursor(
    tmp_path: Path,
) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, arguments))
        if len(host_calls) == 2:
            return {"error": "stale_page_cursor", "message": "Document changed"}
        return {"snapshot_id": f"snapshot-{len(host_calls)}"}

    exit_code, client = _run_page_scenario(
        tmp_path,
        [
            _response("computer_state"),
            _response("computer_state", page_cursor="stale-cursor"),
            _response("computer_state", page_cursor="stale-cursor", max_elements=500),
            LLMResponse(content="Document read.", tool_calls=[], raw={}),
        ],
        host_request,
    )
    assert exit_code == 0
    assert host_calls == [
        ("computer_state", {}),
        ("computer_state", {"page_cursor": "stale-cursor"}),
        ("computer_state", {"max_elements": 500}),
    ]
    assert client.call_records[2]["tool_choice"]["function"]["name"] == "computer_state"
    assert any(
        "Discard the previous pagination results" in str(message.get("content"))
        for message in client.call_records[2]["messages"]
        if message.get("role") == "system"
    )


def test_stale_page_recovery_cannot_be_skipped_by_model_finalization(tmp_path: Path) -> None:
    calls = 0

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"snapshot_id": "before"} if calls == 1 else {"error": "stale_page_cursor"}

    exit_code, client = _run_page_scenario(
        tmp_path,
        [
            _response("computer_state"),
            _response("computer_state", page_cursor="stale"),
            LLMResponse(content="Everything is complete.", tool_calls=[], raw={}),
        ],
        host_request,
    )
    assert exit_code == 1
    assert client.calls == 3
    assert calls == 2


@pytest.mark.parametrize("max_steps,expected_calls", [(10, 3), (1, 1)])
def test_stale_page_recovery_respects_retry_and_turn_budgets(
    tmp_path: Path, max_steps: int, expected_calls: int
) -> None:
    host_calls: list[str] = []

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append(name)
        return {"error": "stale_page_cursor"}

    exit_code, client = _run_page_scenario(
        tmp_path,
        [_response("computer_state", page_cursor=f"stale-{index}") for index in range(3)],
        host_request,
        max_steps=max_steps,
    )
    assert exit_code == 1
    assert len(host_calls) == client.calls == expected_calls


@pytest.mark.parametrize(
    "error",
    [
        "stale_page_cursor",
        "target_not_foreground",
        "computer_use_action_outcome_uncertain",
        "user_takeover",
        "computer_use_route_changed",
        "computer_use_approval_denied",
    ],
)
def test_action_failures_remain_terminal_without_replay(tmp_path: Path, error: str) -> None:
    host_calls: list[str] = []

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append(name)
        if name == "computer_state":
            return {"snapshot_id": "before"}
        return {"error": error, "pre_dispatch": True}

    exit_code, client = _run_page_scenario(
        tmp_path,
        [
            _response("computer_state"),
            _response("computer_action", action="press", element_id="button"),
        ],
        host_request,
    )
    assert exit_code == 1
    assert client.calls == 2
    assert host_calls == ["computer_state", "computer_action"]


def test_stale_page_recovery_refuses_action_before_fresh_observation(tmp_path: Path) -> None:
    host_calls: list[str] = []

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append(name)
        if len(host_calls) == 2:
            return {"error": "stale_page_cursor"}
        return {"snapshot_id": f"snapshot-{len(host_calls)}"}

    exit_code, client = _run_page_scenario(
        tmp_path,
        [
            _response("computer_state"),
            _response("computer_state", page_cursor="stale"),
            _response("computer_action", action="press", element_id="old-button"),
            _response("computer_state"),
            LLMResponse(content="Document read.", tool_calls=[], raw={}),
        ],
        host_request,
    )
    assert exit_code == 0
    assert client.calls == 5
    assert host_calls == ["computer_state", "computer_state", "computer_state"]


def test_undispatched_dom_action_forces_fresh_state_before_final_answer_without_replaying(
    tmp_path: Path,
) -> None:
    calls = []

    def host(name, arguments):
        calls.append((name, dict(arguments)))
        return (
            _undispatched_stale_action()
            if name == "computer_action"
            else {"snapshot_id": f"fresh-{len(calls)}", "source": "chrome-dom"}
        )

    exit_code, client = _run_page_scenario(
        tmp_path,
        [
            _response("computer_state"),
            _response("computer_action", action="press", element_id="old-link"),
            _response("computer_state"),
            LLMResponse(content="The source is unavailable.", tool_calls=[], raw={}),
        ],
        host,
    )
    assert exit_code == 0
    assert [name for name, _ in calls] == ["computer_state", "computer_action", "computer_state"]
    assert client.call_records[2]["tool_choice"]["function"]["name"] == "computer_state"


def test_undispatched_action_recovery_cannot_be_skipped_by_a_memory_answer(tmp_path: Path) -> None:
    calls = []

    def host(name, arguments):
        calls.append(name)
        return (
            _undispatched_stale_action()
            if name == "computer_action"
            else {"snapshot_id": "before", "source": "chrome-dom"}
        )

    exit_code, client = _run_page_scenario(
        tmp_path,
        [
            _response("computer_state"),
            _response("computer_action", action="press", element_id="old-link"),
            LLMResponse(content="I remember the answer.", tool_calls=[], raw={}),
        ],
        host,
    )
    assert exit_code == 1
    assert client.calls == 3 and calls == ["computer_state", "computer_action"]


@pytest.mark.parametrize("max_steps,model_calls,host_calls", [(10, 6, 5), (2, 2, 2)])
def test_undispatched_action_recovery_keeps_two_read_recovery_and_turn_limits(
    tmp_path: Path, max_steps: int, model_calls: int, host_calls: int
) -> None:
    calls = []

    def host(name, arguments):
        calls.append(name)
        return (
            _undispatched_stale_action()
            if name == "computer_action"
            else {"snapshot_id": f"fresh-{len(calls)}", "source": "chrome-dom"}
        )

    responses = [
        item
        for _ in range(3)
        for item in [
            _response("computer_state"),
            _response("computer_action", action="press", element_id="observed-link"),
        ]
    ]
    exit_code, client = _run_page_scenario(tmp_path, responses, host, max_steps=max_steps)
    assert exit_code == 1
    assert client.calls == model_calls and len(calls) == host_calls
    assert calls == (["computer_state", "computer_action"] * 3)[:host_calls]
    assert calls.count("computer_action") <= 2, (
        "the existing failure budget prevents another dispatch"
    )


@pytest.mark.parametrize("explicit_performed", [False, True])
def test_fresh_observation_recovery_budget_renews_after_successful_navigation(
    tmp_path: Path,
    explicit_performed: bool,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    responses = [_response("computer_action", action="press", element_id="unobserved")]
    expected_calls: list[tuple[str, dict[str, Any]]] = []
    recovery_steps = [1]
    for index in range(3):
        responses.extend(
            [
                _response("computer_state"),
                _response("computer_action", action="press", element_id=f"navigate-{index}"),
                _response("computer_action", action="press", element_id=f"stale-{index}"),
            ]
        )
        expected_calls.extend(
            [
                ("computer_state", {}),
                (
                    "computer_action",
                    {
                        "action": "press",
                        "element_id": f"navigate-{index}",
                        "snapshot_id": f"fresh-{index * 3 + 1}",
                    },
                ),
                (
                    "computer_action",
                    {
                        "action": "press",
                        "element_id": f"stale-{index}",
                        "snapshot_id": f"post-navigation-{index * 3 + 2}",
                    },
                ),
            ]
        )
        recovery_steps.append(len(responses))
    responses.extend(
        [
            _response("computer_state"),
            LLMResponse(
                content="The newly observed source states the indentation rule.",
                tool_calls=[],
                raw={},
            ),
        ]
    )
    expected_calls.append(("computer_state", {}))

    def host(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, dict(arguments)))
        if name == "computer_state":
            return {"snapshot_id": f"fresh-{len(calls)}", "source": "chrome-dom"}
        if arguments["element_id"].startswith("stale-"):
            return _undispatched_stale_action()
        # Match the shipped Chrome navigation receipt, which omits performed/status.
        receipt: dict[str, Any] = {
            "action": "press",
            "semantic": True,
            "navigation": True,
            "source": "chrome-dom",
        }
        if explicit_performed:
            receipt.update(performed=True, status="performed")
        return {
            "receipt": receipt,
            "state": {"snapshot_id": f"post-navigation-{len(calls)}", "source": "chrome-dom"},
        }

    exit_code, client = _run_page_scenario(tmp_path, responses, host, max_steps=16)
    assert exit_code == 0
    assert client.calls == len(responses)
    assert calls == expected_calls, (
        "every dispatch/read is model-requested and uses its own trusted state"
    )
    for step in recovery_steps:
        assert client.call_records[step]["tool_choice"]["function"]["name"] == "computer_state"


@pytest.mark.parametrize(
    "intervening_receipt",
    [
        None,
        {"action": "type", "performed": True},
        {"action": "press", "performed": False},
        {"action": "press", "performed": 1},
        {"action": "press", "performed": True, "status": "uncertain"},
        {"action": "press", "performed": True, "status": "not_dispatched"},
    ],
)
def test_observations_and_nonprogress_receipts_do_not_renew_stale_recovery_budget(
    tmp_path: Path,
    intervening_receipt: dict[str, Any] | None,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    responses: list[LLMResponse] = []
    for index in range(3):
        responses.extend(
            [
                _response("computer_state"),
                _response("computer_wait", milliseconds=1),
                _response("computer_action", action="press", element_id=f"nonprogress-{index}"),
                _response("computer_action", action="press", element_id=f"stale-{index}"),
            ]
        )

    def host(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, dict(arguments)))
        state = {"snapshot_id": f"fresh-{len(calls)}", "source": "chrome-dom"}
        if name != "computer_action":
            return state
        if arguments["element_id"].startswith("stale-"):
            return _undispatched_stale_action()
        return {"receipt": intervening_receipt, "state": state}

    exit_code, client = _run_page_scenario(tmp_path, responses, host, max_steps=16)
    assert exit_code == 1
    assert client.calls == len(responses) == len(calls)
    assert [arguments["element_id"] for name, arguments in calls if name == "computer_action"] == [
        target for index in range(3) for target in (f"nonprogress-{index}", f"stale-{index}")
    ], "distinct stale actions still terminate after two recoveries without progress"


def test_repeated_actions_without_any_observation_keep_recovery_budget(tmp_path: Path) -> None:
    calls: list[str] = []

    def host(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(name)
        return {"snapshot_id": "unexpected"}

    exit_code, client = _run_page_scenario(
        tmp_path,
        [
            _response("computer_action", action="press", element_id=f"unobserved-{index}")
            for index in range(3)
        ],
        host,
    )
    assert exit_code == 1
    assert client.calls == 3
    assert calls == [], "the recovery budget must never cause automatic observation or dispatch"
