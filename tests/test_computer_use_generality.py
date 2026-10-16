import pytest
from test_embedded_browser_visual import image_result, visual_cfg
from test_ide_computer_use_host import _lab_state, _ScriptedComputerUseClient

from alysis_code.agent.browser_start import BrowserStartHandoff, build_computer_start_tools
from alysis_code.agent.computer_use_evidence import observed_facts
from alysis_code.agent.computer_use_tools import build_computer_use_tools
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.embedded_browser_visual import ComputerUseViewer
from alysis_code.llm.types import LLMResponse, ToolCall


def test_repeated_visual_and_subtree_observation_reaches_host(tmp_path):
    calls = []

    def host(name, arguments):
        calls.append((name, arguments))
        return {"snapshot_id": f"state-{len(calls)}", "elements": []}

    tools = build_computer_use_tools(
        host,
        response_mode="host_sanitized_text",
        observation_available=True,
        viewer=ComputerUseViewer(root=tmp_path, cfg=visual_cfg()),
    )
    tools["computer_state"].run({})
    tools["computer_state"].run(
        {"include_image": True, "element_id": "deep-control", "max_elements": 30}
    )
    assert calls[-1][1] == {
        "include_image": True,
        "element_id": "deep-control",
        "max_elements": 30,
        "snapshot_id": "state-1",
    }
    tools["computer_state"].run({"include_image": True})
    assert calls[-1][1] == {"include_image": True}


@pytest.mark.parametrize(
    "name,value", [("Τίτλος", "Καλημέρα"), ("名前", "研究"), ("الاسم", "مرحبا"), ("Итог", "347")]
)
def test_goal_facts_are_unicode_exact_and_do_not_certify_task_completion(name, value):
    fact = {"role": "AXTextField", "name": name, "property": "value", "expected": value}
    state = {
        "snapshot_id": "fresh",
        "elements": [{"role": "AXTextField", "label": name, "value": value}],
    }
    result = observed_facts(state, [fact])
    assert result["status"] == "satisfied"
    assert result["goal_completion"] == "not_certified"
    assert observed_facts({**state, "elements": []}, [fact])["status"] == "inconclusive"
    assert (
        observed_facts({**state, "elements": state["elements"] * 2}, [fact])["status"]
        == "inconclusive"
    )
    assert (
        observed_facts(
            {**state, "elements": [{**state["elements"][0], "value": "different"}]}, [fact]
        )["status"]
        == "not_satisfied"
    )
    assert (
        observed_facts({**state, "elements": [{**state["elements"][0], "secure": True}]}, [fact])[
            "status"
        ]
        == "inconclusive"
    )


def test_discovery_is_read_only_and_start_requires_opaque_host_ids(tmp_path):
    inventory = {
        "version": 1,
        "targets": [
            {
                "target_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "name": "研究 Σημειώσεις",
                "route": "native",
            }
        ],
    }
    calls = []
    tools = build_computer_start_tools(
        lambda args: calls.append(args) or BrowserStartHandoff("bound"), inventory
    )
    assert tools["computer_discover"].run({}) == inventory
    assert calls == []
    args = {
        "target_ids": [inventory["targets"][0]["target_id"]],
        "allowed_actions": [],
        "goal": "Διάβασε τη σημείωση.",
        "constraints": ["Μην πληκτρολογήσεις."],
    }
    assert isinstance(tools["computer_start"].run(args), BrowserStartHandoff)
    assert calls == [args]
    assert "error" in tools["computer_start"].run({**args, "target_ids": ["org.unfamiliar.app"]})
    assert len(calls) == 1
    # A controlled model fixture tests handoff plumbing, not language understanding.
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=3,
        no_log=True,
        api_key_override="fixture",
        computer_discovery=inventory,
        browser_start_host_request=lambda args: BrowserStartHandoff("bound"),
    )
    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                raw={},
                tool_calls=[
                    ToolCall(id="start", name="computer_start", arguments=args),
                    ToolCall(id="must-not-run", name="computer_discover", arguments={}),
                ],
            )
        ]
    )
    session.client = client
    try:
        assert session.run_turn("Διάβασε τη σημείωση. Μην πληκτρολογήσεις τίποτα.") == 0
        assert len(client.calls) == 1
    finally:
        session.close()


def test_native_images_reach_next_model_call_ephemerally_with_fresh_transform(tmp_path):
    images = []

    def host(name, args):
        state = _lab_state(f"fresh-{len(images)}", "READY")
        if args.get("include_image"):
            picture = image_result()["image"]
            images.append(picture["data"])
            state["target"]["bounds"] = {"x": 100, "y": 200, "width": 200, "height": 100}
            state.update(
                image=picture,
                coordinate_transform={
                    "logicalBounds": {"x": 100, "y": 200, "width": 200, "height": 100},
                    "coordinateSpace": "global_display_points",
                },
            )
        return state

    session = create_session(
        cfg=visual_cfg(),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=4,
        no_log=True,
        api_key_override="fixture",
        sensitive_media=True,
        computer_use_host_request=host,
        computer_use_response_mode="host_sanitized_text",
        computer_observation_available=True,
    )
    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                raw={},
                tool_calls=[
                    ToolCall(id="image1", name="computer_state", arguments={"include_image": True})
                ],
            ),
            LLMResponse(
                content="",
                raw={},
                tool_calls=[
                    ToolCall(id="image2", name="computer_state", arguments={"include_image": True})
                ],
            ),
            LLMResponse(content="Observed the selected window.", raw={}, tool_calls=[]),
        ]
    )
    session.client = client
    try:
        assert session.run_turn("Read the selected window.") == 0
        assert len(images) == 2
        for call in client.calls[1:]:
            parts = [
                p
                for m in call["messages"]
                if isinstance(m.get("content"), list)
                for p in m["content"]
            ]
            assert sum(p.get("type") == "image_url" for p in parts) == 1
            assert any("global_display_points" in p.get("text", "") for p in parts)
        assert all(
            images[0] not in m.get("content", "")
            for call in client.calls
            for m in call["messages"]
            if isinstance(m.get("content"), str)
        )
        assert all(not getattr(t.visual_delivery, "_pending", {}) for t in session.tools.values())
    finally:
        session.close()


def test_fact_payload_and_image_fallback_stay_bounded():
    fact = {"role": "AXTextField", "name": "Name", "property": "value", "expected": "Done"}
    state = {
        "snapshot_id": "fresh",
        "elements": [{"role": "AXTextField", "name": "Name", "value": {"unexpected": "data"}}],
    }
    result = observed_facts(state, [fact])
    assert result["status"] == "inconclusive"
    assert "unexpected" not in str(result)
    calls = []
    tools = build_computer_use_tools(
        lambda n, a: calls.append(a) or state,
        response_mode="host_sanitized_text",
        observation_available=True,
    )
    observed = tools["computer_state"].run({"include_image": True})
    assert calls == [{"include_image": False}]
    assert observed["snapshot_id"] == "fresh"
    assert observed["visual"]["status"] == "unavailable"


def test_embedded_goal_assertions_use_new_host_snapshot_without_forwarding_model_predicates(
    tmp_path,
):
    calls = []
    state = {
        "tab_id": "tab",
        "snapshot_id": "fresh",
        "url": "https://example.test/",
        "title": "Fixture",
        "text": "",
        "links": [],
        "next_offset": None,
        "elements": [{"ref": "observed-name", "role": "textbox", "name": "名前", "value": "研究"}],
        "viewport": {"width": 800, "height": 600},
    }

    def host(action, args):
        calls.append((action, args))
        return state

    session = create_session(
        cfg=visual_cfg(),
        root=tmp_path,
        mode="auto",
        yes=True,
        no_log=True,
        api_key_override="fixture",
        host_action_capabilities=("browser.snapshot",),
        max_steps=3,
        host_action_handler=host,
    )
    try:
        result = session.tools["embedded_browser_snapshot"].run(
            {
                "tab_id": "tab",
                "assertions": [
                    {"role": "textbox", "name": "名前", "property": "value", "expected": "研究"}
                ],
            }
        )
        assert calls == [("browser.snapshot", {"tab_id": "tab"})]
        assert result["goal_evidence"]["status"] == "satisfied"
        assert result["goal_evidence"]["goal_completion"] == "not_certified"
    finally:
        session.close()


def test_discovery_wire_selection_is_bound_private_and_single_use(tmp_path):
    import threading

    from test_browser_start import binding, respond
    from test_ide_computer_use_host import _bridge_session, _wait_for_event

    from alysis_code.ide.stdio_bridge import _browser_start_host_param

    bridge, out, session, job = _bridge_session(tmp_path)
    inventory = {
        "version": 1,
        "targets": [
            {
                "target_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "name": "研究",
                "route": "native",
                "availability": "installed",
            }
        ],
    }
    session.sensitive_media = False
    session.computer_use_host = None
    session.browser_start_host = _browser_start_host_param(
        {"browser_start_host": {**binding(), "discovery": inventory}}, request_id="r"
    )
    selected = {
        "target_ids": [inventory["targets"][0]["target_id"]],
        "allowed_actions": ["activate_application"],
        "goal": "Open 研究",
        "constraints": ["Do not type"],
    }
    result = []

    def invoke():
        session.surface.with_job(job.job_id)
        session.surface.emit_tool_call_started("call-1", "computer_start", "{}")
        try:
            result.append(bridge._request_browser_start(session, selected))
        except BaseException as e:
            result.append(e)

    worker = threading.Thread(target=invoke)
    worker.start()
    try:
        event = _wait_for_event(out, "browser_start_request")
        assert event["payload"]["selection"] == selected
        assert "url" not in event["payload"]
        identities = {
            k: event["payload"][k]
            for k in ("request_id", "operation_id", "host_nonce", "tool_call_id")
        }
        identities.update(session_id=event["session_id"], job_id=event["job_id"])
        respond(bridge, identities, allow=True)
        worker.join(2)
        assert isinstance(result[0], BrowserStartHandoff)
        assert "browser_start_request" not in str(bridge._event_buffers)
        assert (
            bridge._request_browser_start(session, selected)["error"] == "browser_start_unavailable"
        )
    finally:
        job.cancellation_event.set()
        worker.join(2)


@pytest.mark.parametrize("property", ["value", "enabled", "focused"])
@pytest.mark.parametrize("state", ["error", "unsupported", "no_value", "unrepresentable"])
def test_native_failed_properties_never_prove_an_empty_or_false_result(property, state):
    attribute = {"value": "AXValue", "enabled": "AXEnabled", "focused": "AXFocused"}[property]
    expected = "" if property == "value" else False
    fact = {
        "role": "AXStaticText",
        "name": "Αποτέλεσμα",
        "property": property,
        "expected": expected,
    }
    node = {
        "role": "AXStaticText",
        "title": "Αποτέλεσμα",
        property: expected,
        "attribute_reads": {attribute: {"state": state, "failure": state == "error"}},
    }
    observation = {"snapshot_id": "fresh", "elements": [node]}
    assert observed_facts(observation, [fact])["status"] == "inconclusive"
    node["attribute_reads"] = {}
    assert observed_facts(observation, [fact])["status"] == "satisfied"


def test_partial_tree_can_prove_a_readable_fact_but_never_a_truncated_prefix():
    fact = {"role": "AXStaticText", "name": "研究結果", "property": "value", "expected": "a" * 500}
    node = {"role": "AXStaticText", "label": "研究結果", "value": "a" * 500}
    state = {
        "snapshot_id": "fresh",
        "elements": [node],
        "incomplete": True,
        "coverage": {"complete": False, "scope": "subtree"},
    }
    result = observed_facts(state, [fact])
    assert result["status"] == "satisfied"
    assert result["observation_incomplete"] is True
    assert result["goal_completion"] == "not_certified"
    for attribute in ("AXValue", "AXDescription"):
        node["truncated_attributes"] = [attribute]
        assert observed_facts(state, [fact])["status"] == "inconclusive"
    node.pop("truncated_attributes")
    node["attribute_reads"] = {"AXChildren": {"failure": True}}
    assert observed_facts(state, [fact])["status"] == "satisfied"


def test_fact_evidence_uses_an_independent_read_after_action_and_does_not_echo_predictions():
    requests = []
    current = {"value": "Before"}

    def host(name, args):
        requests.append((name, args.copy()))
        if name == "computer_action":
            current["value"] = "Actual new result"
            return {"dispatched": True}
        return {
            "snapshot_id": f"read-{len(requests)}",
            "elements": [
                {
                    "id": "result",
                    "role": "AXStaticText",
                    "label": "Result",
                    "value": current["value"],
                }
            ],
        }

    tools = build_computer_use_tools(host, response_mode="host_sanitized_text")
    tools["computer_state"].run({})
    tools["computer_action"].run({"action": "press", "element_id": "result"})
    fact = {
        "role": "AXStaticText",
        "name": "Result",
        "property": "value",
        "expected": "Model prediction",
    }
    result = tools["computer_state"].run({"assertions": [fact]})
    assert requests[-1] == ("computer_state", {})
    assert result["goal_evidence"]["snapshot_id"] == "read-3"
    assert result["goal_evidence"]["status"] == "not_satisfied"
    assert result["goal_evidence"]["facts"][0]["actual"] == "Actual new result"
