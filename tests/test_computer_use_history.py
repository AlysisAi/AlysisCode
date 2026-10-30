"""Stale legacy Windows Computer Use states are stubbed once a newer state arrives."""

from __future__ import annotations

import json
from typing import Any

import pytest

from alysis_code.agent.computer_use_history import (
    COMPUTER_USE_OBSERVATION_METADATA_KEY,
    SUPERSEDED_OBSERVATION_NOTE,
    observation_snapshot_id,
    supersede_computer_use_observations,
)
from alysis_code.agent.llm_calls import _request_messages_with_volatile_suffix
from alysis_code.mcp.client import McpToolCallResult
from alysis_code.mcp.manager import McpToolBinding
from alysis_code.mcp.native_relay_guard import legacy_windows_observation_tool
from alysis_code.request_estimation import estimate_message_tokens

_STATE_TOOL = "mcp__alysis_computer__computer_state"
_ACTION_TOOL = "mcp__alysis_computer__computer_action"
_OBSERVATION_TOOLS = (_STATE_TOOL, _ACTION_TOOL)


class _FakeVisual:
    def stage(self, images: list[dict[str, Any]], *, source: str) -> dict[str, Any]:
        return {"visual_delivered": True, "visual_delivery_id": f"vd-{len(images)}"}


class _FakeClient:
    def __init__(self) -> None:
        self.next: McpToolCallResult | None = None

    def call_tool(self, *, tool_name: str, arguments: dict[str, Any]) -> McpToolCallResult:
        assert self.next is not None
        return self.next


def _paint_state(snapshot: str, step: int) -> dict[str, Any]:
    """A Paint window state as the helper's observe.rs shapes it (~2,000 element chars)."""

    elements = ["e0 Window @0,0,1280,800: Untitled - Paint"]
    names = [
        "File",
        "Edit",
        "View",
        "Select",
        "Crop",
        "Resize",
        "Rotate",
        "Pencil",
        "Fill",
        "Text",
        "Eraser",
        "Color picker",
        "Magnifier",
        "Brushes",
        "Shapes",
        "Line",
        "Curve",
        "Rectangle",
        "Oval",
        "Size",
        "Color 1",
        "Color 2",
        "Black",
        "White",
        "Gray",
        "Red",
        "Orange",
        "Yellow",
        "Green",
        "Blue",
        "Indigo",
        "Purple",
        "Edit colors",
        "Layers",
        "Zoom in",
        "Zoom out",
        "Canvas",
        "Status bar",
    ]
    for index, name in enumerate(names, start=1):
        x = 10 + (index * 37) % 1200
        elements.append(f"e{index} Button press @{x},{40 + index},{32},{32}: {name}")
    elements.append(f"e{len(names) + 1} Text value @10,780,300,20: Step {step} pixels 1280 x 800")
    return {
        "snapshot_id": snapshot,
        "app": "mspaint.exe",
        "window": "Untitled - Paint",
        "size": [1280, 800],
        "elements": elements,
    }


def _result(text_value: dict[str, Any], *, is_error: bool = False) -> McpToolCallResult:
    text = json.dumps(text_value, separators=(",", ":"))
    content: list[dict[str, Any]] = [
        {"type": "text", "text": text, "summary": f"text({len(text)} chars)"}
    ]
    if not is_error:
        content.append({"type": "image", "data": "AAAA", "mime_type": "image/png"})
    return McpToolCallResult(
        is_error=is_error,
        structured_content=None,
        content=tuple(content),
        content_summary="; ".join(item.get("summary", "image") for item in content),
        extracted_text=text,
        raw_payload={},
    )


def _tool_content(binding: McpToolBinding, client: _FakeClient, value: dict[str, Any], **kw) -> str:
    client.next = _result(value, **kw)
    payload = binding.run({})
    # The turn loop sends exactly this canonical JSON as the tool message content.
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _bindings() -> tuple[McpToolBinding, McpToolBinding, _FakeClient]:
    client = _FakeClient()
    common = dict(
        server_id="alysis-computer",
        description="",
        parameters={"type": "object"},
        client=client,
        visual_delivery=_FakeVisual(),
    )
    state = McpToolBinding(tool_name="computer_state", tool_alias=_STATE_TOOL, **common)
    action = McpToolBinding(tool_name="computer_action", tool_alias=_ACTION_TOOL, **common)
    return state, action, client


def _assistant(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def _cu_history(steps: int, *, supersede: bool) -> tuple[list[dict[str, Any]], list[int]]:
    """A synthetic CU drawing turn: state, then actions with re-observations between."""

    state_binding, action_binding, client = _bindings()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "Stable host policy"},
        {"role": "user", "content": "Draw a house in Paint."},
    ]
    per_call_history_tokens: list[int] = []
    for step in range(1, steps + 1):
        per_call_history_tokens.append(
            estimate_message_tokens(_request_messages_with_volatile_suffix(messages=messages))
        )
        call_id = f"call-{step}"
        snapshot = f"s{step:04d}"
        if step == 1 or step % 2 == 0:
            messages.append(_assistant(call_id, _STATE_TOOL, {"bundle_id": "mspaint.exe"}))
            content = _tool_content(state_binding, client, _paint_state(snapshot, step))
        else:
            arguments = {
                "snapshot_id": f"s{step - 1:04d}",
                "action": "drag",
                "x": 200 + step,
                "y": 300,
                "to_x": 400 + step,
                "to_y": 300 + step,
            }
            messages.append(_assistant(call_id, _ACTION_TOOL, arguments))
            receipt = {"action": "drag", "semantic": False, "method": "send_input"}
            content = _tool_content(
                action_binding,
                client,
                {"receipt": receipt, "state": _paint_state(snapshot, step)},
            )
        messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
        if supersede:
            supersede_computer_use_observations(
                messages,
                latest_tool_call_id=call_id,
                observation_tool_names=_OBSERVATION_TOOLS,
                current_step_call_ids=(call_id,),
            )
    return messages, per_call_history_tokens


def _tool_message(messages: list[dict[str, Any]], call_id: str) -> dict[str, Any]:
    return next(m for m in messages if m.get("role") == "tool" and m["tool_call_id"] == call_id)


def test_synthetic_forty_step_history_keeps_only_latest_state_and_shrinks(capsys) -> None:
    before, before_calls = _cu_history(40, supersede=False)
    after, after_calls = _cu_history(40, supersede=True)
    before_tokens = estimate_message_tokens(before)
    after_tokens = estimate_message_tokens(after)
    tool_tokens = lambda msgs: estimate_message_tokens([m for m in msgs if m["role"] == "tool"])  # noqa: E731

    with capsys.disabled():
        print(
            "\nCU-HISTORY-40 "
            + json.dumps(
                {
                    "history_tokens_before": before_tokens,
                    "history_tokens_after": after_tokens,
                    "observation_tokens_before": tool_tokens(before),
                    "observation_tokens_after": tool_tokens(after),
                    "cumulative_history_tokens_40_calls_before": sum(before_calls),
                    "cumulative_history_tokens_40_calls_after": sum(after_calls),
                    "last_call_history_tokens_before": before_calls[-1],
                    "last_call_history_tokens_after": after_calls[-1],
                }
            )
        )

    assert after_tokens < before_tokens * 0.35
    assert sum(after_calls) < sum(before_calls) * 0.4
    # The latest state is complete; every older one is a stub with its pairing intact.
    assert len(after) == len(before)
    assert [m.get("tool_call_id") for m in after] == [m.get("tool_call_id") for m in before]
    assert _tool_message(after, "call-40") == _tool_message(before, "call-40")
    assert observation_snapshot_id(_tool_message(after, "call-40")["content"]) == "s0040"
    for step in range(1, 40):
        content = _tool_message(after, f"call-{step}")["content"]
        assert SUPERSEDED_OBSERVATION_NOTE in content
        assert "e7 Button" not in content
        payload = json.loads(content)
        assert payload["is_error"] is False
        text = payload["content"][0]["text"]
        if payload["tool_name"] == "computer_action":
            # The receipt survives verbatim, still fenced as untrusted server text.
            assert "[MCP_UNTRUSTED_TEXT]" in text
            assert '"receipt":{"action":"drag","semantic":false,"method":"send_input"}' in text
        else:
            assert payload["tool_name"] == "computer_state"
            assert text == SUPERSEDED_OBSERVATION_NOTE


def test_each_observation_is_stubbed_once_so_the_prefix_moves_one_boundary_per_step() -> None:
    state_binding, action_binding, client = _bindings()
    messages: list[dict[str, Any]] = [{"role": "user", "content": "Draw."}]
    previous: list[dict[str, Any]] | None = None
    for step in range(1, 8):
        call_id = f"call-{step}"
        messages.append(_assistant(call_id, _STATE_TOOL, {}))
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": _tool_content(state_binding, client, _paint_state(f"s{step}", step)),
            }
        )
        supersede_computer_use_observations(
            messages,
            latest_tool_call_id=call_id,
            observation_tool_names=_OBSERVATION_TOOLS,
            current_step_call_ids=(call_id,),
        )
        if previous is not None:
            changed = [
                index
                for index, (old, new) in enumerate(zip(previous, messages, strict=False))
                if old != new
            ]
            # Only the previously latest observation changed; everything before is stable.
            assert changed == [len(previous) - 1]
        previous = json.loads(json.dumps(messages))


def test_errors_receipts_current_step_pages_and_other_tools_are_kept() -> None:
    state_binding, action_binding, client = _bindings()
    error_content = _tool_content(
        action_binding,
        client,
        {
            "error": {
                "code": "key_target_not_routable",
                "message": "refused",
                "dispatch": "not_dispatched",
            }
        },
        is_error=True,
    )
    first_page = _tool_content(state_binding, client, _paint_state("s1", 1))
    second_page = _tool_content(
        state_binding, client, {**_paint_state("s1", 1), "page_cursor": "s1:40"}
    )
    parallel = _tool_content(state_binding, client, _paint_state("s2", 2))
    latest = _tool_content(state_binding, client, _paint_state("s3", 3))
    other_tool = json.dumps(
        {"server_id": "other", "tool_name": "computer_state", "is_error": False}
    )
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Use Paint."},
        _assistant("c-err", _ACTION_TOOL, {}),
        {"role": "tool", "tool_call_id": "c-err", "content": error_content},
        _assistant("c-p1", _STATE_TOOL, {}),
        {"role": "tool", "tool_call_id": "c-p1", "content": first_page},
        _assistant("c-p2", _STATE_TOOL, {"page_cursor": "s1:40"}),
        {"role": "tool", "tool_call_id": "c-p2", "content": second_page},
        _assistant("c-other", "fs_read", {}),
        {"role": "tool", "tool_call_id": "c-other", "content": other_tool},
        {"role": "user", "content": "Keep going."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c-par",
                    "type": "function",
                    "function": {"name": _STATE_TOOL, "arguments": "{}"},
                },
                {
                    "id": "c-new",
                    "type": "function",
                    "function": {"name": _STATE_TOOL, "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "c-par", "content": parallel},
    ]
    # A page of the same snapshot does not supersede the earlier page.
    assert (
        supersede_computer_use_observations(
            messages[:7],
            latest_tool_call_id="c-p2",
            observation_tool_names=_OBSERVATION_TOOLS,
            current_step_call_ids=("c-p2",),
        )
        == 0
    )
    messages.append({"role": "tool", "tool_call_id": "c-new", "content": latest})
    stubbed = supersede_computer_use_observations(
        messages,
        latest_tool_call_id="c-new",
        observation_tool_names=_OBSERVATION_TOOLS,
        current_step_call_ids=("c-par", "c-new"),
    )

    assert stubbed == 2
    assert _tool_message(messages, "c-err")["content"] == error_content
    assert _tool_message(messages, "c-other")["content"] == other_tool
    assert _tool_message(messages, "c-par")["content"] == parallel
    assert _tool_message(messages, "c-new")["content"] == latest
    assert SUPERSEDED_OBSERVATION_NOTE in _tool_message(messages, "c-p1")["content"]
    assert SUPERSEDED_OBSERVATION_NOTE in _tool_message(messages, "c-p2")["content"]
    # Idempotent: a second pass changes nothing.
    snapshot = json.loads(json.dumps(messages))
    assert (
        supersede_computer_use_observations(
            messages,
            latest_tool_call_id="c-new",
            observation_tool_names=_OBSERVATION_TOOLS,
            current_step_call_ids=("c-new",),
        )
        == 1  # c-par is a past step now and gets stubbed; nothing else changes.
    )
    assert [m for m in messages if m.get("tool_call_id") != "c-par"] == [
        m for m in snapshot if m.get("tool_call_id") != "c-par"
    ]


def test_new_result_without_state_does_not_supersede_the_previous_state() -> None:
    state_binding, action_binding, client = _bindings()
    first = _tool_content(state_binding, client, _paint_state("s1", 1))
    closed = _tool_content(
        action_binding, client, {"receipt": {"outcome": "window_closed"}, "state": None}
    )
    messages: list[dict[str, Any]] = [
        _assistant("c1", _STATE_TOOL, {}),
        {"role": "tool", "tool_call_id": "c1", "content": first},
        _assistant("c2", _ACTION_TOOL, {}),
        {"role": "tool", "tool_call_id": "c2", "content": closed},
    ]
    assert (
        supersede_computer_use_observations(
            messages,
            latest_tool_call_id="c2",
            observation_tool_names=_OBSERVATION_TOOLS,
            current_step_call_ids=("c2",),
        )
        == 0
    )
    assert messages[1]["content"] == first


@pytest.mark.parametrize(
    ("server_id", "tool_name", "expected"),
    [
        ("alysis-computer", "computer_state", True),
        ("alysis-computer", "computer_action", True),
        ("alysis-computer", "computer_wait", False),
        ("other-server", "computer_state", False),
    ],
)
def test_only_legacy_windows_state_tools_are_marked(server_id, tool_name, expected) -> None:
    assert legacy_windows_observation_tool(server_id, tool_name) is expected
    assert COMPUTER_USE_OBSERVATION_METADATA_KEY == "computer_use_observation"


class _ScriptedCuClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, steps: int) -> None:
        self.steps = steps
        self.requests: list[list[dict[str, Any]]] = []

    def chat(self, *, messages: list[dict[str, Any]], **_kwargs: Any) -> Any:
        from alysis_code.llm.openai_compat import LLMResponse, ToolCall

        self.requests.append(json.loads(json.dumps(messages, default=str)))
        call = len(self.requests)
        if call > self.steps:
            return LLMResponse(content="Drew the house.", tool_calls=[], raw={})
        name = _STATE_TOOL if call % 2 else _ACTION_TOOL
        return LLMResponse(
            content="",
            tool_calls=[ToolCall(id=f"cu-{call}", name=name, arguments={})],
            raw={},
        )


def test_turn_loop_replays_only_the_latest_complete_state(tmp_path) -> None:
    from alysis_code.agent_loop import ToolDef, create_session
    from alysis_code.config import AppConfig

    state_binding, action_binding, client = _bindings()
    counter = {"n": 0}

    def run_state(_args: dict[str, Any]) -> dict[str, Any]:
        counter["n"] += 1
        client.next = _result(_paint_state(f"s{counter['n']}", counter["n"]))
        return state_binding.run({})

    def run_action(_args: dict[str, Any]) -> dict[str, Any]:
        counter["n"] += 1
        client.next = _result(
            {
                "receipt": {"action": "press"},
                "state": _paint_state(f"s{counter['n']}", counter["n"]),
            }
        )
        return action_binding.run({})

    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=True,
        api_key_override="override-key",
        verification_enabled=False,
    )
    metadata = {"tool_type": "mcp", COMPUTER_USE_OBSERVATION_METADATA_KEY: True}
    for name, run in ((_STATE_TOOL, run_state), (_ACTION_TOOL, run_action)):
        session.tools[name] = ToolDef(
            name=name,
            description="Legacy Windows Computer Use.",
            parameters={"type": "object", "properties": {}},
            run=run,
            metadata=dict(metadata),
        )
    session.tool_list = [tool.as_openai_tool() for tool in session.tools.values()]
    scripted = _ScriptedCuClient(steps=4)
    session.client = scripted  # type: ignore[assignment]
    try:
        session.run_turn("Draw a house in Paint.")
    finally:
        session.close()

    final_request = scripted.requests[4]
    tool_results = {
        m["tool_call_id"]: m["content"] for m in final_request if m.get("role") == "tool"
    }
    assert set(tool_results) == {"cu-1", "cu-2", "cu-3", "cu-4"}
    assert observation_snapshot_id(tool_results["cu-4"]) == "s4"
    for call_id in ("cu-1", "cu-2", "cu-3"):
        assert SUPERSEDED_OBSERVATION_NOTE in tool_results[call_id]
        assert "e7 Button" not in tool_results[call_id]
    assert '"receipt":{"action":"press"}' in json.loads(tool_results["cu-2"])["content"][0]["text"]
    # The request before saw its own latest state complete.
    previous = {
        m["tool_call_id"]: m["content"] for m in scripted.requests[3] if m.get("role") == "tool"
    }
    assert observation_snapshot_id(previous["cu-3"]) == "s3"


def test_observation_guidance_is_stable_and_survives_description_budget() -> None:
    from alysis_code.agent.computer_use_tools import COMPUTER_USE_SYSTEM_PROMPT
    from alysis_code.mcp.native_relay_guard import (
        LEGACY_WINDOWS_OBSERVATION_GUIDANCE,
        legacy_windows_tool_guidance,
    )
    from alysis_code.mcp.untrusted_content import build_host_owned_mcp_tool_description

    mac = " ".join(COMPUTER_USE_SYSTEM_PROMPT.split())
    assert (
        "Trust the state and any image returned by the last action; call computer_state only "
        "when that state is missing, stale or uncertain." in mac
    )
    guidance = legacy_windows_tool_guidance("alysis-computer", "computer_action")
    assert guidance == LEGACY_WINDOWS_OBSERVATION_GUIDANCE
    description = build_host_owned_mcp_tool_description(
        server_id="alysis-computer",
        tool_name="computer_action",
        server_description="Act on the latest computer_state. " * 20,
        host_guidance=guidance,
    )
    # Host-authored and placed before server text.
    assert description.index(guidance) < description.index("[mcp-tool-description begin]")
    assert legacy_windows_tool_guidance("alysis-computer", "computer_wait") is None
    assert legacy_windows_tool_guidance("other", "computer_state") is None
    assert build_host_owned_mcp_tool_description(
        server_id="other", tool_name="computer_state", server_description="x"
    ) == build_host_owned_mcp_tool_description(
        server_id="other", tool_name="computer_state", server_description="x", host_guidance=None
    )


def _helper_tool_description(tool_name: str, server_text: str, *, server_id="alysis-computer"):
    """Build the model-facing description exactly as the manager and tools assembly do."""

    from alysis_code.mcp.native_relay_guard import (
        legacy_windows_description_max_chars,
        legacy_windows_tool_guidance,
    )
    from alysis_code.mcp.untrusted_content import build_host_owned_mcp_tool_description

    description = build_host_owned_mcp_tool_description(
        server_id=server_id,
        tool_name=tool_name,
        server_description=server_text,
        host_guidance=legacy_windows_tool_guidance(server_id, tool_name),
        description_max_chars=legacy_windows_description_max_chars(server_id, tool_name),
    )
    return description


def _sized(length: int, marker: str) -> str:
    text = ("Use semantic element ids from the latest snapshot for press and type. " * 80)[
        : length - len(marker)
    ]
    return text + marker


@pytest.mark.parametrize(
    ("tool_name", "length"),
    # PR #230 (bc7f267a) lengths: computer_action 1,939, computer_state 1,019, computer_wait 76.
    [
        ("computer_action", 1939),
        ("computer_state", 1019),
        ("computer_wait", 76),
        ("computer_action", 3000),
    ],
)
def test_helper_tools_show_their_full_description_within_a_finite_budget(
    tmp_path, tool_name, length
) -> None:
    from test_approval_review import _Surface, _tools

    from alysis_code.mcp.manager import McpToolBinding
    from alysis_code.mcp.native_relay_guard import LEGACY_WINDOWS_DESCRIPTION_MAX_CHARS

    server_text = _sized(length, " END-OF-HELPER-CONTRACT")
    other_text = _sized(length, " END-OF-OTHER-CONTRACT")
    client = _FakeClient()

    def binding(server_id: str, alias: str, text: str) -> McpToolBinding:
        return McpToolBinding(
            server_id=server_id,
            tool_name=tool_name,
            tool_alias=alias,
            description=_helper_tool_description(tool_name, text, server_id=server_id),
            parameters={"type": "object", "properties": {}},
            client=client,
        )

    from types import SimpleNamespace

    manager = SimpleNamespace(
        tool_bindings=(
            binding("alysis-computer", f"mcp__alysis_computer__{tool_name}", server_text),
            binding("other-server", f"mcp__other_server__{tool_name}", other_text),
        ),
        active_servers=(),
    )
    tools = _tools(tmp_path, _Surface(allow=True), mode="auto", reviewer=None, mcp_manager=manager)
    helper = tools[f"mcp__alysis_computer__{tool_name}"]
    other = tools[f"mcp__other_server__{tool_name}"]
    helper_text = helper.as_openai_tool()["function"]["description"]
    other_text_seen = other.as_openai_tool()["function"]["description"]

    assert helper.metadata["model_description_max_chars"] == LEGACY_WINDOWS_DESCRIPTION_MAX_CHARS
    assert "END-OF-HELPER-CONTRACT" in helper_text
    assert " ".join(server_text.split()) in helper_text
    assert len(helper_text) <= LEGACY_WINDOWS_DESCRIPTION_MAX_CHARS
    # Still fenced and labelled as server-authored text.
    assert "[mcp-tool-description begin]" in helper_text
    assert "[mcp-tool-description end]" in helper_text
    assert "External MCP tool from server 'alysis-computer'" in helper_text
    # Every other server keeps the ordinary 1,000-character budget.
    assert other.metadata["model_description_max_chars"] == 1000
    assert len(other_text_seen) <= 1000
    if length > 900:
        assert "END-OF-OTHER-CONTRACT" not in other_text_seen
    assert helper.metadata[COMPUTER_USE_OBSERVATION_METADATA_KEY] is (tool_name != "computer_wait")
    assert other.metadata[COMPUTER_USE_OBSERVATION_METADATA_KEY] is False


def test_helper_description_budget_is_finite() -> None:
    from alysis_code.mcp.native_relay_guard import (
        LEGACY_WINDOWS_DESCRIPTION_MAX_CHARS,
        legacy_windows_description_max_chars,
    )

    huge = _helper_tool_description("computer_action", "x " * 10_000)
    assert len(huge) <= LEGACY_WINDOWS_DESCRIPTION_MAX_CHARS
    assert huge.endswith("[mcp-tool-description end]")
    assert legacy_windows_description_max_chars("alysis-computer", "computer_wait") == 4000
    assert legacy_windows_description_max_chars("alysis-computer", "other_tool") is None
    assert legacy_windows_description_max_chars("other", "computer_state") is None


def test_helper_server_instructions_reach_the_prompt_uncut() -> None:
    from alysis_code.mcp.untrusted_content import (
        MCP_UNTRUSTED_TEXT_CHAR_LIMIT,
        server_instructions_context,
    )

    # PR #230 INSTRUCTIONS is 2,558 characters; the shared bound is 12,000.
    instructions = _sized(2558, " END-OF-INSTRUCTIONS")
    context = server_instructions_context({"alysis-computer": instructions})
    assert context is not None
    assert "END-OF-INSTRUCTIONS" in context
    assert "truncated: false" in context
    assert "untrusted reference material" in context
    assert MCP_UNTRUSTED_TEXT_CHAR_LIMIT >= 4 * len(instructions)


def _real_visual_bindings(monkeypatch=None) -> tuple[McpToolBinding, McpToolBinding, _FakeClient]:
    """Bindings with the real McpVisualDelivery channel and a vision-capable model."""

    from test_mcp_visual_delivery import config as vision_config

    from alysis_code.mcp.visual_delivery import McpVisualDelivery

    client = _FakeClient()
    common = dict(
        server_id="alysis-computer",
        description="",
        parameters={"type": "object"},
        client=client,
        visual_delivery=McpVisualDelivery(vision_config()),
    )
    state = McpToolBinding(tool_name="computer_state", tool_alias=_STATE_TOOL, **common)
    action = McpToolBinding(tool_name="computer_action", tool_alias=_ACTION_TOOL, **common)
    return state, action, client


def _with_image(value: dict[str, Any], data: str) -> McpToolCallResult:
    text = json.dumps(value, separators=(",", ":"))
    content = (
        {"type": "text", "text": text, "summary": f"text({len(text)} chars)"},
        {"type": "image", "data": data, "mime_type": "image/png", "summary": "image"},
    )
    return McpToolCallResult(
        is_error=False,
        structured_content=None,
        content=content,
        content_summary=f"text({len(text)} chars); image",
        extracted_text=text,
        raw_payload={},
    )


@pytest.mark.parametrize("tool", ["state", "action"])
def test_superseded_observation_keeps_host_visual_delivery_failure_and_receipt(tool) -> None:
    from test_mcp_visual_delivery import image_data

    state_binding, action_binding, client = _real_visual_bindings()
    binding = state_binding if tool == "state" else action_binding
    name = _STATE_TOOL if tool == "state" else _ACTION_TOOL

    def value(snapshot: str) -> dict[str, Any]:
        state = _paint_state(snapshot, 1)
        return state if tool == "state" else {"receipt": {"action": "press"}, "state": state}

    # Real channel: a broken image is a failed delivery while the state itself succeeds.
    client.next = _with_image(value("s1"), "AAAA")
    failed = binding.run({})
    assert failed["is_error"] is False
    assert failed["visual_delivered"] is False
    assert failed["visual_error"] == "invalid_or_unsupported_image"
    # Real channel: a valid image is a successful delivery with its own receipt.
    client.next = _with_image(value("s2"), image_data())
    delivered = binding.run({})
    assert delivered["visual_delivered"] is True
    client.next = _with_image(value("s3"), image_data())
    latest = binding.run({})

    def dumped(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    messages: list[dict[str, Any]] = [{"role": "user", "content": "Use Paint."}]
    for call_id, payload in (("c1", failed), ("c2", delivered), ("c3", latest)):
        messages.append(_assistant(call_id, name, {}))
        messages.append({"role": "tool", "tool_call_id": call_id, "content": dumped(payload)})
        supersede_computer_use_observations(
            messages,
            latest_tool_call_id=call_id,
            observation_tool_names=_OBSERVATION_TOOLS,
            current_step_call_ids=(call_id,),
        )

    old_failed = json.loads(_tool_message(messages, "c1")["content"])
    old_delivered = json.loads(_tool_message(messages, "c2")["content"])
    # The stale state text is gone; the host's record of what happened is not.
    for stub, original in ((old_failed, failed), (old_delivered, delivered)):
        assert stub["superseded_observation"] is True
        assert "e7 Button" not in json.dumps(stub)
        for key in original:
            if key != "content":
                assert stub[key] == original[key], key
        assert [item for item in stub["content"] if item["type"] == "image"] == [
            item for item in original["content"] if item["type"] == "image"
        ]
    assert old_failed["visual_delivered"] is False
    assert old_failed["visual_error"] == "invalid_or_unsupported_image"
    assert "visual_delivery_id" not in old_failed
    assert old_delivered["visual_delivered"] is True
    assert old_delivered["visual_delivery_id"] == delivered["visual_delivery_id"]
    assert old_delivered["image_count"] == 1
    assert "AAAA" not in json.dumps(old_failed)
    if tool == "action":
        assert '"receipt":{"action":"press"}' in old_failed["content"][0]["text"]
    # Latest is intact and pairing is unchanged; a second pass changes nothing.
    assert _tool_message(messages, "c3")["content"] == dumped(latest)
    snapshot = json.loads(json.dumps(messages))
    assert (
        supersede_computer_use_observations(
            messages,
            latest_tool_call_id="c3",
            observation_tool_names=_OBSERVATION_TOOLS,
            current_step_call_ids=("c3",),
        )
        == 0
    )
    assert messages == snapshot


def test_result_with_structured_content_is_kept_whole() -> None:
    state_binding, _action_binding, client = _bindings()
    first = json.loads(_tool_content(state_binding, client, _paint_state("s1", 1)))
    first["structured_content"] = {"receipt": {"action": "press"}}
    messages: list[dict[str, Any]] = [
        _assistant("c1", _STATE_TOOL, {}),
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(first)},
        _assistant("c2", _STATE_TOOL, {}),
        {
            "role": "tool",
            "tool_call_id": "c2",
            "content": _tool_content(state_binding, client, _paint_state("s2", 2)),
        },
    ]
    supersede_computer_use_observations(
        messages,
        latest_tool_call_id="c2",
        observation_tool_names=_OBSERVATION_TOOLS,
        current_step_call_ids=("c2",),
    )
    assert messages[1]["content"] == json.dumps(first)


@pytest.mark.parametrize("length", [3000, 3600, 3700, 4000, 4001, 6000, 20_000])
def test_final_helper_description_keeps_both_fences_at_and_above_the_cap(length) -> None:
    from alysis_code.agent_loop import ToolDef
    from alysis_code.mcp.native_relay_guard import LEGACY_WINDOWS_DESCRIPTION_MAX_CHARS

    for tool_name in ("computer_action", "computer_state", "computer_wait"):
        server_text = _sized(length, " END-OF-HELPER-CONTRACT")
        description = _helper_tool_description(tool_name, server_text)
        model_text = ToolDef(
            name=f"mcp__alysis_computer__{tool_name}",
            description=description,
            parameters={"type": "object"},
            run=lambda _args: {},
            metadata={
                "tool_type": "mcp",
                "model_description_max_chars": LEGACY_WINDOWS_DESCRIPTION_MAX_CHARS,
            },
        ).as_openai_tool()["function"]["description"]

        assert len(description) <= LEGACY_WINDOWS_DESCRIPTION_MAX_CHARS
        assert len(model_text) <= LEGACY_WINDOWS_DESCRIPTION_MAX_CHARS
        # The final projection never slices the fenced text: both markers survive.
        assert model_text == " ".join(description.split())
        assert "[mcp-tool-description begin]" in model_text
        assert model_text.endswith("[mcp-tool-description end]")
        assert "External MCP tool from server 'alysis-computer'" in model_text
        if length <= 3500:
            assert "END-OF-HELPER-CONTRACT" in model_text
