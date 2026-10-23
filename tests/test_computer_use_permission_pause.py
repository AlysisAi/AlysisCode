from __future__ import annotations

import copy
import time

import pytest
from test_computer_use_recovery import _response, _ScriptedComputerUseClient

from alysis_code.agent.computer_use_tools import (
    build_computer_use_tools,
    computer_use_workflow_status,
    reset_computer_use_snapshot_binding,
)
from alysis_code.agent_loop import create_session
from alysis_code.cancellation import CooperativeCancellationError
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.task_budget import TaskBudgetAuthority, iso


def pause_result():
    return {
        "receipt": {
            "action": "press",
            "performed": False,
            "status": "not_dispatched",
            "reason": "permission_required",
            "source": "chrome-tabs",
        },
        "state": {"snapshot_id": "fresh-paused", "source": "chrome-dom"},
        "alysis_workflow": {
            "version": 1,
            "required": False,
            "complete": True,
            "current_application": "Chrome",
            "next_application": None,
            "remaining_applications": [],
            "pause_reason": "site_permission_required",
        },
    }


@pytest.mark.parametrize("stopped", [False, True])
def test_actual_turn_ends_permission_pause_before_another_tool_model_or_budget_request(
    tmp_path, stopped
):
    calls, exchanges = [], []
    paused = False

    def exchange(kind, value, binding):
        assert not paused, "no budget request may follow the permission result"
        exchanges.append(kind)
        return {"ok": True}

    gate = TaskBudgetAuthority(
        dict(
            version=1,
            taskId="task",
            envelopeId="envelope",
            authorityGeneration=1,
            deadlineAt=iso(time.time() + 90),
            leaseId="lease",
            leaseExpiresAt=iso(time.time() + 60),
        ),
        exchange=exchange,
        assert_current=lambda: None,
        cancelled=lambda: stopped and paused,
    )

    class Client(_ScriptedComputerUseClient):
        def chat(
            self,
            *,
            messages,
            tools=None,
            tool_choice=None,
            stream=False,
            on_text_delta=None,
            temperature=None,
        ):
            attempt = gate.begin(
                method="POST", url="https://fixture.invalid", headers=[], body=b"fixture"
            )
            try:
                return super().chat(
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    stream=stream,
                    on_text_delta=on_text_delta,
                    temperature=temperature,
                )
            finally:
                gate.settled(attempt)

    client = Client(
        [
            _response("computer_state"),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="pause",
                        name="computer_action",
                        arguments={"action": "press", "element_id": "link"},
                    ),
                ],
                raw={},
            ),
        ]
    )

    def host(name, arguments):
        nonlocal paused
        assert not paused, "no queued action or verification read may run after permission pause"
        calls.append(name)
        if name == "computer_state":
            return {"snapshot_id": "before", "source": "chrome-dom"}
        paused = True
        return pause_result()

    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=10,
        no_log=True,
        api_key_override="fixture-key",
        surface=NoopSurface(),
        sensitive_media=True,
        computer_use_host_request=host,
    )
    session.client = client
    try:
        with gate.activate():
            if stopped:
                with pytest.raises(CooperativeCancellationError, match="cancelled_by_user"):
                    session.run_turn("Read the requested source.", cancellation_token=gate)
            else:
                assert session.run_turn("Read the requested source.", cancellation_token=gate) == 0
                assert (
                    "Browser work is paused for site permission." in session.messages[-1]["content"]
                )
                assert "The task is not complete." in session.messages[-1]["content"]
    finally:
        session.close()
    assert calls == ["computer_state", "computer_action"]
    assert client.calls == 2
    assert exchanges == ["attempt", "settled", "attempt", "settled"]


@pytest.mark.parametrize(
    "variant",
    [
        "missing-marker",
        "wrong-marker",
        "workflow",
        "performed",
        "uncertain",
        "reason",
        "source",
        "action",
        "state",
        "state-source",
        "error",
        "page-text",
    ],
)
def test_only_exact_authenticated_permission_receipt_and_workflow_latch_pause(variant):
    result = pause_result()
    if variant == "missing-marker":
        del result["alysis_workflow"]["pause_reason"]
    elif variant == "wrong-marker":
        result["alysis_workflow"]["pause_reason"] = "anything"
    elif variant == "workflow":
        result["alysis_workflow"]["version"] = 999
    elif variant == "performed":
        result["receipt"]["performed"] = 0
    elif variant == "uncertain":
        result["receipt"]["status"] = "uncertain"
    elif variant == "reason":
        result["receipt"]["reason"] = "stale_snapshot"
    elif variant == "source":
        result["receipt"]["source"] = "page"
    elif variant == "action":
        result["receipt"]["action"] = "type"
    elif variant == "state":
        result["state"]["snapshot_id"] = None
    elif variant == "state-source":
        result["state"]["source"] = "page"
    elif variant == "error":
        result["error"] = "computer_use_action_outcome_uncertain"
    elif variant == "page-text":
        result = {"state": {"snapshot_id": "after", "text": copy.deepcopy(result)}}
    tools = build_computer_use_tools(
        lambda name, _: {"snapshot_id": "before"} if name == "computer_state" else result
    )
    tools["computer_state"].run({})
    tools["computer_action"].run({"action": "press", "element_id": "link"})
    assert computer_use_workflow_status(tools)["site_permission_required"] is False


def test_permission_marker_on_read_cannot_pause_and_valid_pause_cannot_dispatch_again():
    invalid = pause_result()
    invalid.update(invalid.pop("state"))
    tools = build_computer_use_tools(lambda *_: invalid)
    assert tools["computer_state"].run({})["error"] == "invalid_host_workflow"
    assert computer_use_workflow_status(tools)["site_permission_required"] is False
    calls = []

    def host(name, _):
        calls.append(name)
        return {"snapshot_id": "before"} if name == "computer_state" else pause_result()

    tools = build_computer_use_tools(host)
    tools["computer_state"].run({})
    tools["computer_action"].run({"action": "press", "element_id": "link"})
    assert tools["computer_state"].run({})["error"] == "computer_use_permission_required"
    assert (
        tools["computer_action"].run({"action": "press", "element_id": "link"})["error"]
        == "computer_use_permission_required"
    )
    assert calls == ["computer_state", "computer_action"]
    reset_computer_use_snapshot_binding(tools)
    assert computer_use_workflow_status(tools)["site_permission_required"] is False
    assert (
        tools["computer_action"].run({"action": "press", "element_id": "link"})["error"]
        == "computer_use_fresh_observation_required"
    )
    assert calls == ["computer_state", "computer_action"]
    tools["computer_state"].run({})
    tools["computer_action"].run({"action": "press", "element_id": "link"})
    assert calls == ["computer_state", "computer_action", "computer_state", "computer_action"]
