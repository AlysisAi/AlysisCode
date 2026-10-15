import pytest

from alysis_code.agent.tools_assembly import _built_in_tool_exposed_in_mode
from alysis_code.capabilities import DESKTOP_FEATURES
from alysis_code.embedded_browser import BROWSER_TOOLS, BROWSER_WRITE_ACTIONS
from alysis_code.host_actions import (
    HOST_ACTION_TOOL_NAMES,
    HostActionError,
    normalize_host_action_arguments,
    normalize_host_action_result,
)


def test_pre_chat_routing_can_discover_embedded_browser_support():
    assert "embedded-browser-host-v1" in DESKTOP_FEATURES


@pytest.mark.parametrize("spec", BROWSER_TOOLS)
def test_browser_tools_are_negotiated_and_only_reads_available_in_readonly(spec):
    assert HOST_ACTION_TOOL_NAMES[spec[0]] == spec[1]
    assert _built_in_tool_exposed_in_mode(tool_name=spec[1], mode="readonly") == (
        spec[0] not in BROWSER_WRITE_ACTIONS
    )
    assert not _built_in_tool_exposed_in_mode(tool_name=spec[1], mode="readonly", subagent_depth=1)


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "javascript:alert(1)", "https://user:password@example.com", "https://"],
)
def test_only_browser_urls(url):
    with pytest.raises(HostActionError):
        normalize_host_action_arguments("browser.open", {"url": url})


@pytest.mark.parametrize("offset", [-1, True, 2000001, "0"])
def test_pagination_validation(offset):
    with pytest.raises(HostActionError):
        normalize_host_action_arguments("browser.read", {"tab_id": "tab", "offset": offset})


def test_page_contract():
    page = {
        "tab_id": "tab",
        "url": "https://example.com/",
        "text": "Rendered page",
        "links": [],
        "next_offset": None,
    }
    assert normalize_host_action_result("browser.open", page) == page
    with pytest.raises(HostActionError):
        normalize_host_action_result("browser.read", {**page, "text": "x" * 6001})


@pytest.mark.parametrize(
    "action,args",
    [
        ("browser.click", {"ref": "control"}),
        ("browser.click", {"x": 40, "y": 50}),
        ("browser.type", {"ref": "control", "text": "Καλημέρα"}),
        ("browser.select", {"ref": "control", "values": ["opt"]}),
        ("browser.scroll", {"x": 0, "y": 700}),
        ("browser.key", {"key": "Shift+Tab"}),
    ],
)
def test_interactions_accept_only_explicit_observation_bound_actions(action, args):
    arguments = {"tab_id": "tab", "snapshot_id": "observation", **args}
    assert normalize_host_action_arguments(action, arguments) == arguments
    with pytest.raises(HostActionError):
        normalize_host_action_arguments(
            action, {key: value for key, value in arguments.items() if key != "snapshot_id"}
        )


@pytest.mark.parametrize(
    "action,args",
    [
        ("browser.click", {}),
        ("browser.click", {"ref": "control", "x": 1, "y": 2}),
        ("browser.click", {"x": 1}),
        ("browser.click", {"x": -1, "y": 2}),
        ("browser.click", {"selector": "#submit"}),
        ("browser.type", {"ref": "control", "text": "x" * 4001}),
        ("browser.select", {"ref": "control", "values": []}),
        ("browser.select", {"ref": "control", "values": [True]}),
        ("browser.scroll", {"x": True, "y": 1}),
        ("browser.key", {"key": "Meta+Q"}),
    ],
)
def test_interaction_rejects_unbounded_or_ambiguous_actions(action, args):
    with pytest.raises(HostActionError):
        normalize_host_action_arguments(
            action, {"tab_id": "tab", "snapshot_id": "observation", **args}
        )


def test_snapshot_contract_rejects_duplicate_refs_and_unbounded_metadata():
    page = {
        "tab_id": "tab",
        "url": "https://example.com/",
        "text": "Rendered page",
        "links": [],
        "next_offset": None,
        "snapshot_id": "snapshot",
        "elements": [{"ref": "e1", "role": "button", "name": "Continue"}],
        "viewport": {"width": 800, "height": 600, "scroll_x": 0, "scroll_y": 0},
    }
    assert normalize_host_action_result("browser.snapshot", page) == page
    for invalid in (
        {"snapshot_id": ""},
        {"elements": page["elements"] * 2},
        {"viewport": {"width": True, "height": 600}},
    ):
        with pytest.raises(HostActionError):
            normalize_host_action_result("browser.snapshot", {**page, **invalid})
