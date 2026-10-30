"""A running app with no open window is a selectable target that the plan must open.

Covers the engine bridge parser for host inventories and the exact label contract around it.
No real apps, providers or desktop input are used.
"""

from __future__ import annotations

import threading

import pytest

from alysis_code.agent.browser_start import BrowserStartHandoff, build_computer_start_tools
from alysis_code.ide.protocol import ProtocolError
from alysis_code.ide.stdio_bridge import _browser_start_host_param

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

WINDOWLESS = {
    "target_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    "name": "TextEdit",
    "route": "native",
    "availability": "no_window",
}
RUNNING = {
    "target_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "name": "Calculator",
    "route": "native",
}


def _bind(targets):
    from test_browser_start import binding

    return _browser_start_host_param(
        {"browser_start_host": {**binding(), "discovery": {"version": 1, "targets": targets}}},
        request_id="r",
    )


def test_the_bridge_accepts_no_window_for_native_apps_and_refuses_unknown_availability():
    bound = _bind([RUNNING, WINDOWLESS])
    assert bound["discovery"]["targets"][1]["availability"] == "no_window"
    for availability in ("hidden", "running", "", "NO_WINDOW", None, 1, ["no_window"]):
        with pytest.raises(ProtocolError):
            _bind([RUNNING, {**WINDOWLESS, "availability": availability}])
    with pytest.raises(ProtocolError):
        _bind([{**WINDOWLESS, "route": "chrome"}])


def test_a_windowless_app_crosses_the_real_bridge_with_its_exact_host_id(tmp_path):
    from test_browser_start import respond
    from test_ide_computer_use_host import _bridge_session, _wait_for_event

    bridge, out, session, job = _bridge_session(tmp_path)
    session.sensitive_media = False
    session.computer_use_host = None
    session.browser_start_host = _bind([RUNNING, WINDOWLESS])
    tools = build_computer_start_tools(
        lambda selection: bridge._request_browser_start(session, selection),
        session.browser_start_host["discovery"],
    )
    assert tools["computer_discover"].run({})["targets"][1] == {
        "app": "TextEdit",
        "route": "native",
        "application_ref": WINDOWLESS["target_id"],
        "availability": "no_window",
    }
    plan = {
        "goal": "Write the result in TextEdit.",
        "constraints": ["Do not overwrite files."],
        "operates_apps": True,
        "app_steps": [
            {"app": "Calculator", "actions": ["press"]},
            {"app": "TextEdit", "actions": ["activate_application", "set_value"]},
        ],
    }
    result = []

    def invoke():
        session.surface.with_job(job.job_id)
        session.surface.emit_tool_call_started("call-1", "computer_start", "{}")
        try:
            result.append(tools["computer_start"].run(plan))
        except BaseException as e:  # noqa: BLE001
            result.append(e)

    worker = threading.Thread(target=invoke)
    worker.start()
    try:
        event = _wait_for_event(out, "browser_start_request")
        assert event["payload"]["selection"] == {
            "target_ids": [RUNNING["target_id"], WINDOWLESS["target_id"]],
            "allowed_actions": ["press", "set_value", "activate_application"],
            "goal": plan["goal"],
            "constraints": plan["constraints"],
        }
        identities = {
            k: event["payload"][k]
            for k in ("request_id", "operation_id", "host_nonce", "tool_call_id")
        }
        identities.update(session_id=event["session_id"], job_id=event["job_id"])
        respond(bridge, identities, allow=True)
        worker.join(2)
        assert isinstance(result[0], BrowserStartHandoff)
    finally:
        worker.join(2)


def test_repeated_names_are_numbered_and_each_label_selects_exactly_one_app():
    twins = [
        {
            "target_id": f"{c * 8}-{c * 4}-4{c * 3}-8{c * 3}-{c * 12}",
            "name": "Editor",
            "route": "native",
        }
        for c in "cd"
    ]
    calls = []
    tools = build_computer_start_tools(
        lambda selection: calls.append(selection), {"version": 1, "targets": twins}
    )
    assert [app["app"] for app in tools["computer_discover"].run({})["targets"]] == [
        "Editor (1)",
        "Editor (2)",
    ]
    plan = {
        "goal": "Read it.",
        "constraints": [],
        "operates_apps": False,
        "app_steps": [{"app": "Editor", "actions": []}],
    }
    assert tools["computer_start"].run(plan)["error"] == "computer_start_invalid_selection", (
        "no copy owns the plain name"
    )
    tools["computer_start"].run({**plan, "app_steps": [{"app": "Editor (2)", "actions": []}]})
    assert calls == [
        {
            "target_ids": [twins[1]["target_id"]],
            "allowed_actions": [],
            "goal": "Read it.",
            "constraints": [],
        }
    ]
    # A real app already named like a numbered copy keeps its own label; numbering skips it.
    taken = [
        *twins,
        {
            "target_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "name": "Editor (1)",
            "route": "native",
        },
    ]
    labels = [
        app["app"]
        for app in build_computer_start_tools(lambda _s: None, {"version": 1, "targets": taken})[
            "computer_discover"
        ].run({})["targets"]
    ]
    assert labels == ["Editor (2)", "Editor (3)", "Editor (1)"] and len(set(labels)) == 3
