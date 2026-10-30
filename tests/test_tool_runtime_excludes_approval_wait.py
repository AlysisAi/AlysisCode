"""Time spent waiting on an approval prompt was billed to the tool.

`Delete File (39.8s)` where the delete took milliseconds and ~39s was the human
deciding. The turn loop now snapshots the surface's cumulative approval-wait
around each tool call: ``ToolEndEvent.elapsed_ms`` is the tool's ACTIVE
runtime and ``approval_wait_ms`` carries the wait for separate disclosure
("(94ms · approval 39.7s)").
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.surface import ApprovalDecision, ApprovalRequest
from alysis_code.surface.noop_surface import NoopSurface


class _FakeClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, stream, on_text_delta, temperature
        response = self._responses[self.calls]
        self.calls += 1
        return response


class _WaitingSurface(NoopSurface):
    """Approves after a real sleep, accounting the wait like the interactive
    surfaces do (TuiSurface / RichSurface both keep approval_wait_ms_total)."""

    def __init__(self, wait_seconds: float) -> None:
        self._wait_seconds = wait_seconds
        self.approval_wait_ms_total = 0
        self.approvals: list[ApprovalRequest] = []
        self.tool_end_events: list[Any] = []

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self.approvals.append(request)
        started = time.perf_counter()
        time.sleep(self._wait_seconds)
        self.approval_wait_ms_total += int((time.perf_counter() - started) * 1000)
        return ApprovalDecision(allow=True)

    def on_tool_end(self, event: Any) -> None:
        self.tool_end_events.append(event)


def test_tool_elapsed_excludes_approval_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Measure approval wait versus a host write, independent of OS worker availability/startup.
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    surface = _WaitingSurface(wait_seconds=0.4)
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=5,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        session_id_override="f7-approval-wait",
        surface=surface,
    )
    fake_client = _FakeClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={"path": "note.txt", "content": "hello\n"},
                    )
                ],
                raw={},
            ),
            LLMResponse(content="Wrote the note.", tool_calls=[], raw={}),
        ]
    )
    session.client = fake_client  # type: ignore[assignment]

    try:
        session.run_turn("write hello to note.txt")
    finally:
        session.close()

    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hello\n"
    assert [request.kind for request in surface.approvals] == ["fs_write"]
    events = [event for event in surface.tool_end_events if event.name == "fs_write"]
    assert len(events) == 1
    event = events[0]
    # The human waited ~400ms; the write itself is milliseconds. Pre-fix,
    # elapsed_ms carried the whole 400ms+.
    assert event.approval_wait_ms >= 300
    assert event.elapsed_ms < 300
    assert event.elapsed_ms >= 0


def test_tui_surface_accumulates_approval_wait() -> None:
    from alysis_code.cli_impl.tui.surface import TuiSurface
    from alysis_code.cli_impl.tui.transcript import TuiTranscript

    def _slow_ui(request: ApprovalRequest) -> ApprovalDecision:
        time.sleep(0.12)
        return ApprovalDecision(allow=True)

    surface = TuiSurface(TuiTranscript(), request_approval_ui=_slow_ui)
    request = ApprovalRequest(kind="fs_write", reason="r", preview="p", files=["x.txt"])

    decision = surface.request_approval(request)

    assert decision.allow is True
    assert surface.approval_wait_ms_total >= 100
