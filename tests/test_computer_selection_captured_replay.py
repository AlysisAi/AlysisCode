"""Replay of operation 8af1961a through the OpenAI Responses adapter.

The provider is an httpx mock. Every raw HTTP request is kept with its path; model turns
(POST /responses) consume the scripted calls in order and token-count requests
(POST /responses/input_tokens) are counted separately. The mock does not honor tool_choice, so
each turn's forced choice is asserted next to the call the script returned. No real provider, app
or desktop is used: a pass here is controlled evidence, not provider acceptance.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from alysis_code.agent.browser_start import BrowserStartHandoff
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.surface.noop_surface import NoopSurface

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/computer_selection/op_8af1961a.json").read_text()
)
REQUEST = (
    "Open Calculator and calculate 12345 × 6789. Write the calculation and result in a new TextEdit document. "
    "Save it as “Calculation.txt” inside a new Desktop folder named “Alysis Test October 12”. "
    "Close and reopen the document, then confirm what was saved."
)


def responses_session(
    tmp_path: Path, inventory: dict[str, Any], script: list[Any], host, max_steps: int = 8
):
    """A real OpenAIResponsesClient whose transport replays `script`, one item per model turn."""
    log: dict[str, list[dict[str, Any]]] = {"raw": [], "turns": [], "token_counts": []}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        log["raw"].append({"method": request.method, "path": request.url.path, "body": body})
        if request.url.path.endswith("/responses/input_tokens"):
            log["token_counts"].append(body)
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 1000}
            )
        if not request.url.path.endswith("/responses"):
            return httpx.Response(404, json={"error": {"message": "unexpected path"}})
        log["turns"].append(body)
        index = len(log["turns"])
        if index > len(script):
            return httpx.Response(400, json={"error": {"message": "script exhausted"}})
        item = script[index - 1]
        if isinstance(item, str):
            output = {
                "type": "message",
                "id": f"msg-{index}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": item, "annotations": []}],
            }
        else:
            name, arguments = item
            output = {
                "type": "function_call",
                "id": f"fc-{index}",
                "call_id": f"call-{index}",
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
                "status": "completed",
            }
        response = {
            "id": f"resp-{index}",
            "object": "response",
            "status": "completed",
            "model": "gpt-test",
            "output": [output],
        }
        if not body.get("stream"):
            return httpx.Response(200, json=response)
        event = {"type": "response.completed", "response": response}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"event: response.completed\ndata: {json.dumps(event)}\n\n".encode(),
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-test",
        transport=httpx.MockTransport(handler),
    )
    session = create_session(
        cfg=AppConfig(model="gpt-test", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=max_steps,
        no_log=True,
        api_key_override="fixture",
        surface=NoopSurface(),
        computer_discovery=inventory,
        browser_start_host_request=host,
    )
    session.client = client
    return session, log


def provider_tool_outputs(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The tool results exactly as serialized into one provider request."""
    return [
        json.loads(item["output"])
        for item in body["input"]
        if item.get("type") == "function_call_output"
    ]


def forced(body: dict[str, Any]) -> str | None:
    choice = body.get("tool_choice")
    return choice.get("name") if isinstance(choice, dict) else None


def run(tmp_path, inventory, script, max_steps: int = 8):
    dispatched: list[dict[str, Any]] = []
    session, log = responses_session(
        tmp_path,
        inventory,
        script,
        lambda args: dispatched.append(copy.deepcopy(args)) or BrowserStartHandoff("bound"),
        max_steps,
    )
    try:
        exit_code = session.run_turn(REQUEST)
    finally:
        session.close()
    # Every raw request is accounted for: model turns plus token counts, nothing else.
    assert len(log["raw"]) == len(log["turns"]) + len(log["token_counts"])
    assert all(entry["method"] == "POST" for entry in log["raw"])
    return exit_code, dispatched, log


def test_the_captured_sequence_reaches_the_provider_whole_and_never_reaches_the_desktop(tmp_path):
    inventory = FIXTURE["inventory"]
    first, second, third = FIXTURE["computer_start_calls"]
    script = [
        ("computer_discover", {}),
        ("computer_start", first),
        ("computer_start", second),
        ("computer_discover", {}),
        ("computer_start", third),
    ]
    exit_code, dispatched, log = run(tmp_path, inventory, script)
    assert exit_code == 1 and dispatched == []
    assert len(log["turns"]) == len(script), (
        "five model turns; the retry limit ends the request after the third start"
    )
    discovered = provider_tool_outputs(log["turns"][1])[0]
    # The provider received all 107 captured apps; the 24-item activity view is display-only.
    assert len(discovered["targets"]) == 107
    assert "TextEdit" not in [app["app"] for app in discovered["targets"]], (
        "the captured host list had no TextEdit"
    )
    assert all(
        set(target) <= {"app", "route", "availability", "application_ref"}
        for target in discovered["targets"]
    ), "discovery exposes only labels, availability and read-only identity references"
    assert [target["application_ref"] for target in discovered["targets"]] == [
        target["target_id"] for target in inventory["targets"]
    ]
    # The captured calls use the retired UUID and duplicate-declaration contract: each is a local,
    # bounded correction that re-lists the same captured apps, never an admission.
    corrections = [provider_tool_outputs(body)[-1] for body in log["turns"][2:4]]
    assert [c["error"] for c in corrections] == ["computer_start_invalid_selection"] * 2
    assert all(
        c["recovery"] == "correct_selection_not_dispatched" and len(c["available_targets"]) == 107
        for c in corrections
    )
    # Forced choices, turn by turn. After an invalid selection the request's list is delivered again;
    # this scripted provider ignores tool_choice, so turn 3 returned the captured start instead. Once the
    # list was delivered (turn 4), only a corrected selection is asked for.
    assert [forced(body) for body in log["turns"]] == [
        None,
        None,
        "computer_discover",
        "computer_discover",
        "computer_start",
    ]
    assert [item[0] for item in script] == [
        "computer_discover",
        "computer_start",
        "computer_start",
        "computer_discover",
        "computer_start",
    ]


def _with_running_windowless_textedit(inventory: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The captured list plus TextEdit as the repaired host reports a running app with no window.

    Synthetic (source-level) data: the captured run never listed TextEdit.
    """
    repaired = copy.deepcopy(inventory)
    textedit = {
        "target_id": str(uuid.UUID(int=0x7E57ED17, version=4)),
        "name": "TextEdit",
        "route": "native",
        "availability": "no_window",
    }
    running = [t for t in repaired["targets"] if "availability" not in t]
    repaired["targets"].insert(len(running), textedit)
    return repaired, textedit["target_id"]


def test_the_repaired_list_admits_the_planned_apps_with_exactly_the_planned_actions(tmp_path):
    inventory, textedit = _with_running_windowless_textedit(FIXTURE["inventory"])
    captured = FIXTURE["computer_start_calls"][2]
    # The captured third plan, by app label, with TextEdit where the model had used Messages.
    labels = {
        "4833b0a6-d1c7-4193-bb2d-f74edddc4c64": "Calculator",
        "199ea371-e9d1-4b13-abba-3d0848f251c2": "TextEdit",
        "b29132c2-573e-496a-84eb-adf56a0005cf": "Finder",
    }
    plan = {
        "goal": captured["goal"],
        "constraints": captured["constraints"],
        "operates_apps": True,
        "app_steps": [
            {"app": labels[step["target_id"]], "actions": step["actions"]}
            for step in captured["app_steps"]
        ],
    }
    exit_code, dispatched, log = run(
        tmp_path, inventory, [("computer_discover", {}), ("computer_start", plan)]
    )
    assert exit_code == 0 and len(log["turns"]) == 2
    listed = {app["app"]: app for app in provider_tool_outputs(log["turns"][1])[0]["targets"]}
    assert listed["TextEdit"] == {
        "app": "TextEdit",
        "route": "native",
        "availability": "no_window",
        "application_ref": textedit,
    }
    assert dispatched == [
        {
            "target_ids": [
                "4833b0a6-d1c7-4193-bb2d-f74edddc4c64",
                textedit,
                "b29132c2-573e-496a-84eb-adf56a0005cf",
            ],
            # Exactly the union of the step actions. The captured declaration's extra focus, set_value
            # and click are not granted, and nothing is added.
            "allowed_actions": ["press", "type", "key", "activate_application"],
            "goal": captured["goal"],
            "constraints": captured["constraints"],
        }
    ]


def test_a_windowless_first_app_must_be_opened_by_its_step(tmp_path):
    inventory, _ = _with_running_windowless_textedit(FIXTURE["inventory"])
    plan = {
        "goal": "Write the result in TextEdit.",
        "constraints": [],
        "operates_apps": True,
        "app_steps": [{"app": "TextEdit", "actions": ["set_value"]}],
    }
    exit_code, dispatched, log = run(
        tmp_path,
        inventory,
        [
            ("computer_discover", {}),
            ("computer_start", plan),
            ("computer_start", plan),
            ("computer_start", plan),
        ],
    )
    assert exit_code == 1 and dispatched == []
    corrections = [provider_tool_outputs(body)[-1] for body in log["turns"][2:]]
    assert {c["error"] for c in corrections} == {"computer_start_scope_inconsistent"}
    assert "no_window" in corrections[0]["message"]
    assert [forced(body) for body in log["turns"][2:]] == ["computer_start", "computer_start"]
