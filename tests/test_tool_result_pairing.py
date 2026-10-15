"""Every assistant tool call reaches the provider with its result directly after it.

Seen live in the desktop app on deepseek-flash (2026-10-09): requests were rejected with HTTP 400
"An assistant message with 'tool_calls' must be followed by tool messages responding to each
'tool_call_id'". In both session logs a ``blast_radius_scope_advisory`` controller note was
appended after the first call's result of a two-call batch (fs_edit + fs_delete; fs_list +
fs_read), so the note sat between the batch's results; the second session then failed on the
next turn as well.
"""

from __future__ import annotations

from alysis_code.agent.llm_calls import (
    UNAVAILABLE_TOOL_RESULT_CONTENT,
    _request_messages_with_volatile_suffix,
    _with_tool_results_after_tool_calls,
)


def _call(call_id: str, name: str = "fs_read") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _assistant(*call_ids: str, content: str = "") -> dict:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [_call(call_id) for call_id in call_ids],
    }


def _result(call_id: str, content: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _assert_provider_valid(messages: list[dict]) -> None:
    """The OpenAI-compatible rule: each tool_calls message is followed by one tool message per call."""
    for index, message in enumerate(messages):
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not calls:
            continue
        expected = {call["id"] for call in calls}
        following = messages[index + 1 : index + 1 + len(expected)]
        assert [item.get("role") for item in following] == ["tool"] * len(expected), messages
        assert {item.get("tool_call_id") for item in following} == expected


def test_a_well_formed_history_is_unchanged() -> None:
    history = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "Fix calc.py"},
        _assistant("a", "b"),
        _result("a"),
        _result("b"),
        {"role": "system", "content": "advisory after the batch"},
        {"role": "assistant", "content": "Done."},
    ]
    assert _with_tool_results_after_tool_calls(history) == history


def test_a_note_appended_inside_a_batch_moves_after_its_results() -> None:
    # fs_edit then fs_delete in one batch: the edit's advisories landed before the delete's result.
    history = [
        {"role": "user", "content": "Fix calc.py"},
        _assistant("edit", "delete"),
        _result("edit"),
        {"role": "system", "content": "regression baseline advisory"},
        {"role": "system", "content": "blast radius advisory"},
        _result("delete", "Blocked by the approval reviewer"),
    ]
    paired = _with_tool_results_after_tool_calls(history)
    _assert_provider_valid(paired)
    assert [item.get("content") for item in paired[2:]] == [
        "ok",
        "Blocked by the approval reviewer",
        "regression baseline advisory",
        "blast radius advisory",
    ]


def test_a_stopped_batch_gets_a_placeholder_result_before_the_next_turn() -> None:
    history = [
        {"role": "user", "content": "List every file"},
        _assistant("list", "read"),
        _result("list"),
        {"role": "user", "content": "Say hi"},
    ]
    paired = _with_tool_results_after_tool_calls(history)
    _assert_provider_valid(paired)
    assert paired[3] == {
        "role": "tool",
        "tool_call_id": "read",
        "content": UNAVAILABLE_TOOL_RESULT_CONTENT,
    }
    assert paired[-1] == {"role": "user", "content": "Say hi"}
    # Also when nothing returned and the next assistant message follows directly.
    _assert_provider_valid(
        _with_tool_results_after_tool_calls(
            [_assistant("x"), {"role": "assistant", "content": "next"}]
        )
    )


def test_the_request_builder_sends_paired_results() -> None:
    request = _request_messages_with_volatile_suffix(
        messages=[
            {"role": "user", "content": "go"},
            _assistant("a", "b"),
            _result("a"),
            {"role": "system", "content": "note"},
        ],
        step_system_prompts=["step prompt"],
    )
    _assert_provider_valid(request)
    assert request[-1] == {"role": "system", "content": "step prompt"}
    assert {"role": "system", "content": "note"} in request
