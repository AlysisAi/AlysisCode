"""LR-42 startup consumers, including raw resolved-call provenance; no host effects."""

import copy
from types import SimpleNamespace

import pytest
from test_computer_start_resolved_contract import CORPUS, entry, invoke

from alysis_code import computer_use_platform_policy as policy
from alysis_code.agent.browser_start import (
    BrowserStartHandoff,
    build_computer_start_tools,
    computer_plan_problem,
    computer_start_problem,
)


@pytest.fixture(autouse=True)
def mac_host(monkeypatch):
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform="darwin"))


def native_plan(action):
    descriptor = copy.deepcopy(CORPUS["bases"]["descriptor"])
    descriptor["discovery"]["targets"] = [descriptor["discovery"]["targets"][0]]
    plan = {
        "goal": "Perform the requested action in Alpha.",
        "constraints": [],
        "operates_apps": True,
        "app_steps": [{"app": "Alpha", "actions": [action]}],
    }
    labels = {"Alpha": descriptor["discovery"]["targets"][0]}
    assert computer_start_problem(plan, labels) is None
    assert computer_plan_problem(plan, labels) is None
    return descriptor, plan


def consumer(route, descriptor, plan):
    if route == "resolved":
        e, calls, cfg = entry(descriptor)
        return lambda: invoke(e, cfg, plan), calls, None
    calls = []

    def request(selection):
        calls.append(selection)
        return BrowserStartHandoff("original")

    tool = build_computer_start_tools(request, descriptor["discovery"])["computer_start"]
    actions = tool.as_openai_tool()["function"]["parameters"]["properties"]["app_steps"]["items"][
        "properties"
    ]["actions"]["items"]["enum"]
    return lambda: tool.run(plan), calls, actions


@pytest.mark.parametrize("route", ["resolved", "discovery"])
@pytest.mark.parametrize(
    "action", ["press", "focus", "click", "set_value", "type", "key", "scroll", "drag"]
)
def test_mac_selected_native_plan_reaches_original_callback(route, action):
    descriptor, plan = native_plan(action)
    run, calls, advertised = consumer(route, descriptor, plan)
    assert not policy.w11_computer_use_qualified()
    if advertised is not None:
        assert action in advertised
        assert "history_back" not in advertised
    assert type(run()) is BrowserStartHandoff
    assert len(calls) == 1
    selection = calls[0][1] if route == "resolved" else calls[0]
    assert selection["allowed_actions"] == [action]
    assert selection["target_ids"] == [descriptor["discovery"]["targets"][0]["target_id"]]


@pytest.mark.parametrize("route", ["resolved", "discovery"])
def test_native_history_remains_browser_only(route):
    descriptor, plan = native_plan("history_back")
    run, calls, advertised = consumer(route, descriptor, plan)
    result = run()
    assert (
        result["code" if route == "resolved" else "error"]
        == "computer_start_unsupported_native_actions"
    )
    if route == "resolved":
        assert result["kind"] == "selection_correction" and result["not_dispatched"] is True
    assert not calls
    if advertised is not None:
        assert "history_back" not in advertised
        assert "New v2/background" not in result["message"]
        assert "history_back is browser-only" in result["message"]


@pytest.mark.parametrize("route", ["resolved", "discovery"])
@pytest.mark.parametrize("platform", ["win32", "linux", "Darwin", "unknown"])
@pytest.mark.parametrize("after_capture", [False, True])
def test_non_mac_and_changed_platform_refuse_before_callback(
    monkeypatch, route, platform, after_capture
):
    descriptor, plan = native_plan("type")
    if after_capture:
        run, calls, _ = consumer(route, descriptor, plan)
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setenv("ALYSIS_MAC_W7_ENABLED", "1")
    monkeypatch.setenv("ALYSIS_PLATFORM", "darwin")
    from alysis_code.agent.browser_start import native_unsupported_start_actions

    assert native_unsupported_start_actions() == {"type", "key", "scroll", "drag", "history_back"}
    with pytest.raises(ValueError):
        if after_capture:
            run()
        else:
            consumer(route, descriptor, plan)
    if after_capture:
        assert not calls
    assert not policy.w11_computer_use_qualified()
