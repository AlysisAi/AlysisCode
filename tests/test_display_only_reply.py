"""A reply written beside the next-steps call is the turn's answer when the model then says nothing.

Some models put their whole reply in the same response as ``suggest_next_steps`` and end the next
response empty. That used to fail the turn and lose the reply. Only the shape of the calls decides
(every call display-only, text present), never the words, and any other empty response is still an
anomaly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.turn.core import _display_only_reply
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.llm.types import LLMEmptyResponseError, LLMError
from alysis_code.session_store import read_session_events

REPLY = "The CSV is ready: orders.csv has 12 orders."
STEPS = ToolCall(
    id="steps", name="suggest_next_steps", arguments={"steps": ["Add a column", "Make 50 rows"]}
)


def _response(content: str = "", *calls: ToolCall) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=list(calls),
        raw={"choices": [{"finish_reason": "stop"}], "usage": {"completion_tokens": 1}},
    )


class _Client:
    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.requests.append(kwargs)
        assert len(self.requests) <= len(self.responses), "more requests than scripted"
        item = self.responses[len(self.requests) - 1]
        if isinstance(item, Exception):
            raise item
        return item


def _session(tmp_path: Path) -> Any:
    return create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=12,
        no_log=False,
        api_key_override="test-key",
        one_shot_execution=False,
        verification_enabled=False,
        enable_compaction=False,
        session_log_dir_override=tmp_path / "sessions",
    )


def _finals(session: Any) -> list[str]:
    return [
        event["payload"]["content"]
        for event in read_session_events(session.store.path)
        if event["type"] == "final"
    ]


def test_only_display_only_calls_with_text_count() -> None:
    assert _display_only_reply(_response(f"  {REPLY}  ", STEPS)) == REPLY
    assert _display_only_reply(_response("", STEPS)) == ""
    assert _display_only_reply(_response(REPLY)) == ""
    read = ToolCall(id="read", name="fs_read", arguments={"path": "a.txt"})
    assert _display_only_reply(_response(REPLY, STEPS, read)) == ""


@pytest.mark.parametrize(
    "empty",
    [
        LLMEmptyResponseError("returned no assistant text or tool calls (stop_reason=end_turn)"),
        _response(),
    ],
    ids=["provider-error", "finished-empty"],
)
def test_reply_beside_next_steps_is_kept_when_the_model_then_says_nothing(
    tmp_path: Path, empty: Any
) -> None:
    session = _session(tmp_path)
    client = _Client([_response(REPLY, STEPS), empty])
    session.client = client
    try:
        assert session.run_turn("Make a CSV of 12 orders.") == 0
    finally:
        session.close()
    assert len(client.requests) == 2
    assert _finals(session) == [REPLY]


def test_an_empty_response_after_real_work_is_still_an_error(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    session = _session(tmp_path)
    read = ToolCall(id="read", name="fs_read", arguments={"path": "a.txt"})
    client = _Client([_response("Reading the file first.", read), LLMEmptyResponseError("empty")])
    session.client = client
    try:
        with pytest.raises(LLMError):
            session.run_turn("Summarise a.txt.")
    finally:
        session.close()
    assert REPLY not in _finals(session)
