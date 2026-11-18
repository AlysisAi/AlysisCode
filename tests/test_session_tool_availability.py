from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from alysis_code import cli as cli_mod
from alysis_code.agent_loop import ToolDef, create_session
from alysis_code.config import AppConfig
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.surface import ApprovalDecision, ApprovalRequest, NoopSurface
from alysis_code.tools.availability import (
    _reset_tool_availability_for_tests,
    unavailable_tool_result,
)


@pytest.fixture(autouse=True)
def _reset_availability() -> Iterator[None]:
    _reset_tool_availability_for_tests()
    yield
    _reset_tool_availability_for_tests()


class _ApproveSurface(NoopSurface):
    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        _ = request
        return ApprovalDecision(allow=True)


class _ScriptedClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = iter(responses)

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta: Any = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, stream, on_text_delta, temperature
        return next(self._responses)


def _create_test_session(
    *,
    root: Path,
    cfg: AppConfig,
    session_id: str,
    mode: str = "review",
    non_interactive: bool = True,
    subagent_depth: int = 0,
    surface: NoopSurface | None = None,
):  # type: ignore[no-untyped-def]
    return create_session(
        cfg=cfg,
        root=root,
        mode=mode,
        yes=True,
        max_steps=4,
        no_log=True,
        api_key_override="test-key",
        session_log_dir_override=root / "sessions",
        session_id_override=session_id,
        non_interactive=non_interactive,
        verification_enabled=False,
        subagents_enabled=False,
        subagent_depth=subagent_depth,
        surface=surface,
    )


def test_child_session_availability_cannot_disable_parent_dispatch(tmp_path: Path) -> None:
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    parent = _create_test_session(
        root=tmp_path,
        cfg=cfg,
        session_id="availability-parent",
        non_interactive=False,
        surface=_ApproveSurface(),
    )
    child = _create_test_session(
        root=tmp_path,
        cfg=cfg,
        session_id="availability-child",
        mode="readonly",
        non_interactive=True,
        subagent_depth=1,
    )
    try:
        assert "switch_mode" in parent.tools
        assert "switch_mode" not in child.tools
        assert (
            unavailable_tool_result(
                "switch_mode",
                availability=parent.tool_availability_snapshot,
            )
            is None
        )
        assert unavailable_tool_result(
            "switch_mode",
            availability=child.tool_availability_snapshot,
        ) == {
            "status": "tool_unavailable",
            "tool": "switch_mode",
            "reason": "persona modes disabled or non-interactive runtime",
        }

        parent.client = _ScriptedClient(  # type: ignore[assignment]
            [
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="switch-1",
                            name="switch_mode",
                            arguments={"persona": "ask", "reason": "Explain only."},
                        )
                    ],
                    raw={},
                ),
                LLMResponse(content="Done.", tool_calls=[], raw={}),
            ]
        )

        assert parent.run_turn("Switch to the explanation persona.") == 0
        assert parent.persona_switch_state is not None
        assert parent.persona_switch_state.pending == ("ask", "Explain only.")
    finally:
        child.close()
        parent.close()


def test_successful_mode_rebuild_commits_tools_and_availability_together(
    tmp_path: Path,
) -> None:
    cfg = AppConfig(
        model="test-model",
        routing_mode="code_only",
        image_generation={
            "enabled": True,
            "model": "gpt-image-test",
            "base_url": "https://images.example.test/v1",
        },
    )
    session = _create_test_session(
        root=tmp_path,
        cfg=cfg,
        session_id="availability-rebuild-success",
    )
    try:
        original_snapshot = session.tool_availability_snapshot
        assert "image_generate" in session.tools
        assert (
            unavailable_tool_result(
                "image_generate",
                availability=original_snapshot,
            )
            is None
        )

        cli_mod._rebuild_session_tools_for_mode(session=session, mode="readonly")

        assert session.tool_availability_snapshot is not original_snapshot
        assert "image_generate" not in session.tools
        unavailable = unavailable_tool_result(
            "image_generate",
            availability=session.tool_availability_snapshot,
        )
        assert unavailable is not None
        assert unavailable["status"] == "tool_unavailable"
        assert "readonly" in unavailable["reason"]
    finally:
        session.close()


def test_failed_mode_rebuild_keeps_previous_tools_and_availability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    session = _create_test_session(
        root=tmp_path,
        cfg=cfg,
        session_id="availability-rebuild-failure",
    )
    original_tools = session.tools
    original_tool_list = session.tool_list
    original_snapshot = session.tool_availability_snapshot
    original_conversion = ToolDef.as_openai_tool

    def _fail_schema_conversion(tool: ToolDef) -> dict[str, Any]:
        if tool.name == "fs_read":
            raise RuntimeError("forced schema conversion failure")
        return original_conversion(tool)

    monkeypatch.setattr(ToolDef, "as_openai_tool", _fail_schema_conversion)
    try:
        with pytest.raises(RuntimeError, match="forced schema conversion failure"):
            cli_mod._rebuild_session_tools_for_mode(session=session, mode="readonly")

        assert session.tools is original_tools
        assert session.tool_list is original_tool_list
        assert session.tool_availability_snapshot is original_snapshot
    finally:
        session.close()
