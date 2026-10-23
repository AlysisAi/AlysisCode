from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from alysis_code.agent_loop import AgentRuntimeError, build_tools, create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import SessionStore, read_session_events
from alysis_code.surface import ApprovalDecision, ApprovalRequest
from alysis_code.surface.hidden_surface import HiddenApprovalSurface
from alysis_code.surface.noop_surface import NoopSurface


def _store(root: Path) -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=root / "sessions",
        session_id="review-test",
        cwd=str(root),
        repo_root=str(root),
    )


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


def test_review_mode_fs_write_requires_approval_and_does_not_write(tmp_path: Path) -> None:
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        surface=NoopSurface(),
        store=_store(tmp_path),
        mode="review",
        yes=False,
    )

    with pytest.raises(AgentRuntimeError, match="User declined: fs_write"):
        tools["fs_write"].run({"path": "blocked.txt", "content": "x"})

    assert (tmp_path / "blocked.txt").exists() is False


def test_review_mode_fs_edit_requires_approval_and_does_not_write(tmp_path: Path) -> None:
    target = tmp_path / "blocked.txt"
    target.write_text("alpha\n", encoding="utf-8")
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        surface=NoopSurface(),
        store=_store(tmp_path),
        mode="review",
        yes=False,
    )

    with pytest.raises(AgentRuntimeError, match="User declined: fs_edit"):
        tools["fs_edit"].run(
            {
                "path": "blocked.txt",
                "edits": [{"op": "replace_exact", "target": "alpha", "replacement": "beta"}],
            }
        )

    assert target.read_text(encoding="utf-8") == "alpha\n"


def test_run_turn_stops_without_retry_when_approval_declined(tmp_path: Path) -> None:
    target = tmp_path / "blocked.txt"
    target.write_text("alpha\n", encoding="utf-8")
    sessions_dir = tmp_path / "sessions"

    class DenyingSurface(NoopSurface):
        def __init__(self) -> None:
            self.approvals: list[ApprovalRequest] = []
            self.assistant_done: list[str] = []

        def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
            self.approvals.append(request)
            return ApprovalDecision(allow=False)

        def on_assistant_message_done(self, text: str) -> None:
            self.assistant_done.append(text)

    surface = DenyingSurface()
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=5,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override="approval-decline-terminal",
        surface=surface,
    )
    fake_client = _FakeClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_edit",
                        arguments={
                            "path": "blocked.txt",
                            "edits": [
                                {
                                    "op": "replace_exact",
                                    "target": "alpha",
                                    "replacement": "beta",
                                }
                            ],
                        },
                    )
                ],
                raw={},
            ),
            LLMResponse(content="retrying declined edit", tool_calls=[], raw={}),
        ]
    )
    session.client = fake_client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Change alpha to beta.")
    finally:
        session.close()

    assert exit_code == 1
    assert fake_client.calls == 1
    assert [request.kind for request in surface.approvals] == ["fs_edit"]
    assert target.read_text(encoding="utf-8") == "alpha\n"
    assert surface.assistant_done == [
        "Approval declined for fs_edit. I stopped without retrying that action. "
        "Tell me how you want to proceed."
    ]

    events = list(read_session_events(sessions_dir / "approval-decline-terminal.jsonl"))
    fs_edit_calls = [
        event.get("payload", {})
        for event in events
        if event.get("type") == "tool_call" and event.get("payload", {}).get("name") == "fs_edit"
    ]
    assert len(fs_edit_calls) == 1
    assert fs_edit_calls[0]["tool_call_id"] == "tc1"
    tool_results = [
        event.get("payload", {}).get("result", {})
        for event in events
        if event.get("type") == "tool_result" and event.get("payload", {}).get("name") == "fs_edit"
    ]
    assert tool_results == [
        {
            "status": "approval_declined",
            "approval_declined": True,
            "approval_kind": "fs_edit",
            "message": "User declined: fs_edit",
        }
    ]
    assert any(
        event.get("type") == "approval_declined"
        and event.get("payload", {}).get("tool_name") == "fs_edit"
        for event in events
    )
    assert any(
        event.get("type") == "final"
        and "stopped without retrying" in str(event.get("payload", {}).get("content") or "")
        for event in events
    )
    assert not any(
        event.get("type") == "warning"
        and event.get("payload", {}).get("warning") == "adaptive_temperature_retry"
        for event in events
    )


def test_review_mode_fs_delete_requires_approval_and_does_not_delete(tmp_path: Path) -> None:
    target = tmp_path / "blocked.txt"
    target.write_text("alpha\n", encoding="utf-8")
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        surface=NoopSurface(),
        store=_store(tmp_path),
        mode="review",
        yes=False,
        non_interactive=False,
    )

    with pytest.raises(AgentRuntimeError, match="User declined: fs_delete"):
        tools["fs_delete"].run({"path": "blocked.txt"})

    assert target.exists() is True
    assert target.read_text(encoding="utf-8") == "alpha\n"


def test_review_mode_shell_run_requires_approval(tmp_path: Path) -> None:
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        surface=NoopSurface(),
        store=_store(tmp_path),
        mode="review",
        yes=False,
    )

    # "echo hi" would run unasked: read-only commands need no approval (read_only_commands).
    with pytest.raises(AgentRuntimeError, match="User declined: shell_run"):
        tools["shell_run"].run({"cmd": "make build"})


def test_review_mode_non_interactive_can_use_host_managed_approval(tmp_path: Path) -> None:
    class HostApprovalSurface(NoopSurface):
        host_managed_approvals = True

        def __init__(self) -> None:
            self.requests: list[ApprovalRequest] = []

        def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
            self.requests.append(request)
            return ApprovalDecision(allow=True)

    surface = HostApprovalSurface()
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        surface=surface,
        store=_store(tmp_path),
        mode="review",
        yes=False,
        non_interactive=True,
    )

    tools["fs_write"].run({"path": "allowed.txt", "content": "x"})

    assert (tmp_path / "allowed.txt").read_text(encoding="utf-8") == "x"
    assert len(surface.requests) == 1
    assert surface.requests[0].kind == "fs_write"


def test_review_mode_fs_write_escape_path_rejected_before_approval(tmp_path: Path) -> None:
    class RecordingSurface(NoopSurface):
        def __init__(self) -> None:
            self.approval_calls = 0
            self.patch_events = 0

        def request_approval(self, request):  # type: ignore[no-untyped-def]
            self.approval_calls += 1
            return super().request_approval(request)

        def on_patch_generated(self, event):  # type: ignore[no-untyped-def]
            self.patch_events += 1
            super().on_patch_generated(event)

    surface = RecordingSurface()
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        surface=surface,
        store=_store(tmp_path),
        mode="review",
        yes=False,
    )

    with pytest.raises(AgentRuntimeError, match="Path escapes root"):
        tools["fs_write"].run({"path": "../evil.txt", "content": "x"})

    assert surface.approval_calls == 0
    assert surface.patch_events == 0


def test_hidden_approval_surface_forwards_only_approval_requests() -> None:
    class RecordingSurface(NoopSurface):
        def __init__(self) -> None:
            self.approval_calls = 0
            self.progress_updates = 0
            self.errors = 0

        def on_progress_update(self, message: str) -> None:
            _ = message
            self.progress_updates += 1

        def on_error(self, err: str) -> None:
            _ = err
            self.errors += 1

        def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
            self.approval_calls += 1
            assert request.kind == "fs_write"
            return ApprovalDecision(allow=True)

    parent_surface = RecordingSurface()
    hidden_surface = HiddenApprovalSurface(parent_surface)

    decision = hidden_surface.request_approval(
        ApprovalRequest(
            kind="fs_write",
            reason="nested review write",
            preview="write file",
            files=["demo.txt"],
        )
    )
    hidden_surface.on_progress_update("nested progress")
    hidden_surface.on_error("nested error")

    assert decision.allow is True
    assert parent_surface.approval_calls == 1
    assert parent_surface.progress_updates == 0
    assert parent_surface.errors == 0
