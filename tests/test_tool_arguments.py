"""Tool arguments are checked against the tool's schema before anything reads them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.tool_arguments import (
    conform_tool_arguments,
    invalid_tool_arguments_result,
)
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "start_line": {"type": "integer", "minimum": 1},
        "ratio": {"type": "number"},
        "literal": {"type": "boolean"},
        "globs": {"type": "array", "items": {"type": "string"}},
        "options": {"type": "object"},
        "mode": {"type": "string", "enum": ["log", "show", "blame"]},
        "either": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
        "maybe": {"type": ["integer", "null"]},
    },
    "required": ["path"],
}


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ({"path": "a.py", "start_line": 190}, {"path": "a.py", "start_line": 190}),
        ({"path": "a.py", "start_line": "190"}, {"path": "a.py", "start_line": 190}),
        ({"path": "a.py", "start_line": " 7 "}, {"path": "a.py", "start_line": 7}),
        ({"path": "a.py", "start_line": 12.0}, {"path": "a.py", "start_line": 12}),
        ({"path": "a.py", "ratio": "0.5"}, {"path": "a.py", "ratio": 0.5}),
        ({"path": "a.py", "literal": "TRUE"}, {"path": "a.py", "literal": True}),
        ({"path": 42}, {"path": "42"}),
        ({"path": "a", "globs": "*.py"}, {"path": "a", "globs": ["*.py"]}),
        ({"path": "a", "mode": "Blame"}, {"path": "a", "mode": "blame"}),
        ({"path": "a", "start_line": None}, {"path": "a", "start_line": None}),
        ({"path": "a", "either": [1]}, {"path": "a", "either": [1]}),
        ({"path": "a", "maybe": "3"}, {"path": "a", "maybe": 3}),
        ({"path": "a", "unknown": object}, {"path": "a", "unknown": object}),
        # A schema documents a tool; it does not list everything its handler accepts.
        ({"path": "a", "mode": "diff"}, {"path": "a", "mode": "diff"}),
        ({"start_line": 1}, {"start_line": 1}),
        ({"path": "a", "literal": "yes"}, {"path": "a", "literal": "yes"}),
        ({"path": "a", "options": "x"}, {"path": "a", "options": "x"}),
        ({"path": "a", "globs": {"x": 1}}, {"path": "a", "globs": {"x": 1}}),
        ({"path": ["a", "b"]}, {"path": ["a", "b"]}),
    ],
)
def test_values_the_tools_already_accepted_keep_working(
    given: dict[str, Any], expected: dict[str, Any]
) -> None:
    assert conform_tool_arguments(SCHEMA, given) == (expected, [])


@pytest.mark.parametrize(
    ("given", "problem"),
    [
        ({"path": "a", "start_line": ","}, "start_line must be an integer (got ',')"),
        ({"path": "a", "start_line": True}, "start_line must be an integer (got True)"),
        ({"path": "a", "start_line": 1.5}, "start_line must be an integer (got 1.5)"),
        ({"path": "a", "ratio": "nan"}, "ratio must be a number (got 'nan')"),
        ({"path": "a", "start_line": [3]}, "start_line must be an integer (got [3])"),
        ({"path": "a", "maybe": "three"}, "maybe must be an integer or null (got 'three')"),
    ],
)
def test_non_numbers_for_numeric_fields_are_reported_by_name(
    given: dict[str, Any], problem: str
) -> None:
    _, problems = conform_tool_arguments(SCHEMA, given)
    assert problems == [problem]


def test_the_input_is_not_modified_and_schemaless_tools_are_not_checked() -> None:
    given = {"path": "a", "start_line": "3"}
    conform_tool_arguments(SCHEMA, given)
    assert given == {"path": "a", "start_line": "3"}
    assert conform_tool_arguments({}, {"x": ","}) == ({"x": ","}, [])
    assert conform_tool_arguments(None, {"x": ","}) == ({"x": ","}, [])
    assert conform_tool_arguments(SCHEMA, ["not", "an", "object"]) == (
        {},
        ["arguments must be a JSON object"],
    )


def test_the_result_names_the_tool_and_each_problem() -> None:
    assert invalid_tool_arguments_result("fs_read", ["a", "b"]) == {
        "error": "Invalid arguments for fs_read: a; b",
        "error_code": "invalid_tool_arguments",
        "problems": ["a", "b"],
        "guidance": "Re-issue the call with arguments that match the tool's parameter schema.",
    }


class _ScriptedClient:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.model = "test-model"
        self.temperature = 0.2
        self._responses = responses
        self.calls = 0

    def chat(self, *, messages, tools=None, stream=False, on_text_delta=None, temperature=None):  # type: ignore[no-untyped-def]
        _ = messages, tools, stream, on_text_delta, temperature
        response = self._responses[self.calls]
        self.calls += 1
        return response


def test_a_wrongly_typed_argument_no_longer_ends_the_turn(tmp_path: Path) -> None:
    # The call from the Windows test drive: fs_read with start_line ",". It used to raise
    # ValueError in the read cache and end the turn with terminal_error.
    (tmp_path / "app.ts").write_text("one\ntwo\nthree\n", encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=5,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override="bad-argument",
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1", name="fs_read", arguments={"path": "app.ts", "start_line": ","}
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc2", name="fs_read", arguments={"path": "app.ts", "start_line": "2"}
                    )
                ],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]
    try:
        exit_code = session.run_turn("Read app.ts from line 2.")
    finally:
        session.close()

    assert exit_code == 0
    assert client.calls == 3
    events = list(read_session_events(sessions_dir / "bad-argument.jsonl"))
    assert not [e for e in events if e.get("type") == "terminal_error"]
    recovered = [
        e["payload"] for e in events if e.get("type") == "invalid_tool_arguments_recovered"
    ]
    assert recovered == [
        {
            "tool": "fs_read",
            "tool_call_id": "tc1",
            "step": 1,
            "problems": ["start_line must be an integer (got ',')"],
        }
    ]
    results = [e["payload"]["result"] for e in events if e.get("type") == "tool_result"]
    assert results[0]["error_code"] == "invalid_tool_arguments"
    assert "two" in str(results[1].get("content", "")) and "one" not in str(
        results[1].get("content", "")
    )
