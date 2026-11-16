from __future__ import annotations

import io
import json
import warnings
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from alysis_code.agent_loop import AgentSession, ToolDef
from alysis_code.approval_scope import exact_file_set_scope
from alysis_code.config import AppConfig
from alysis_code.interactive_input_guard import is_interactive_prompt_active
from alysis_code.knowledge_capture import RecordingSurface
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.model_registry import ModelMeta
from alysis_code.session_store import SessionStore
from alysis_code.surface import rich_surface as rich_surface_mod
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.surface.rich_surface import RichSurface
from alysis_code.surface.types import (
    ApprovalDecision,
    ApprovalRequest,
    PatchEvent,
    StatusEvent,
    SubagentEndEvent,
    SubagentStartEvent,
    ToolEndEvent,
    ToolOutputEvent,
    ToolStartEvent,
)
from alysis_code.usage_tracker import UsageSummary


class _RecordingSurface:
    def __init__(self) -> None:
        self.user_messages: list[str] = []
        self.assistant_tokens: list[str] = []
        self.assistant_done: list[str] = []
        self.tool_starts: list[ToolStartEvent] = []
        self.tool_outputs: list[ToolOutputEvent] = []
        self.tool_ends: list[ToolEndEvent] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def on_status_update(self, status: StatusEvent) -> None:
        _ = status

    def on_user_message(self, text: str) -> None:
        self.user_messages.append(text)

    def on_assistant_token(self, delta: str) -> None:
        self.assistant_tokens.append(delta)

    def on_assistant_message_done(self, text: str) -> None:
        self.assistant_done.append(text)

    def on_tool_start(self, event: ToolStartEvent) -> None:
        self.tool_starts.append(event)

    def on_tool_output(self, event: ToolOutputEvent) -> None:
        self.tool_outputs.append(event)

    def on_tool_end(self, event: ToolEndEvent) -> None:
        self.tool_ends.append(event)

    def on_patch_generated(self, event: PatchEvent) -> None:
        _ = event

    def on_warning(self, warning: str) -> None:
        self.warnings.append(warning)

    def on_error(self, err: str) -> None:
        self.errors.append(err)

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        _ = request
        return ApprovalDecision(allow=True)


class _FakeClient:
    model = "test-model"
    temperature = 1.0

    def __init__(self) -> None:
        self._calls = 0

    def chat(self, **_kwargs: Any) -> LLMResponse:
        self._calls += 1
        if self._calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call_1", name="echo_tool", arguments={"msg": "hello"})],
                raw={},
            )
        return LLMResponse(content="done", tool_calls=[], raw={})


class _FakeRegistry:
    def get(self, model_name: str) -> ModelMeta:
        return ModelMeta(
            model_name=model_name,
            context_window_tokens=8192,
            max_output_tokens=2048,
            input_cost_per_token=None,
            output_cost_per_token=None,
            raw_metadata={},
            source="fallback",
        )


def _store(root: Path) -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=root / "sessions",
        session_id="s1",
        cwd=str(root),
        repo_root=str(root),
    )


def _make_test_agent_session(
    *,
    root: Path,
    surface: _RecordingSurface,
    tool: ToolDef,
) -> AgentSession:
    return AgentSession(
        cfg=AppConfig(model="test-model"),
        root=root,
        mode="auto",
        yes=True,
        stream=False,
        routing_mode="code_only",
        max_steps=3,
        console=Console(file=io.StringIO(), force_terminal=False),
        surface=surface,
        store=_store(root),
        client=_FakeClient(),  # type: ignore[arg-type]
        model_registry=_FakeRegistry(),  # type: ignore[arg-type]
        usage_summary=UsageSummary(),
        usage_role="main",
        tool_output_offloader=None,
        conversation_compactor=None,
        tool_output_offload_enabled=False,
        conversation_summarization_enabled=False,
        compaction_profile="chat",
        tools={"echo_tool": tool},
        tool_list=[tool.as_openai_tool()],
        messages=[{"role": "system", "content": "s"}],
    )


def test_agent_session_routes_tool_events_to_surface(tmp_path: Path) -> None:
    surface = _RecordingSurface()
    tool = ToolDef(
        name="echo_tool",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        run=lambda _args: {"ok": True},
    )

    session = _make_test_agent_session(root=tmp_path, surface=surface, tool=tool)
    try:
        code = session.run_turn("hello world")
    finally:
        session.close()

    assert code == 0
    assert surface.user_messages == ["hello world"]
    assert len(surface.tool_starts) == 1
    assert surface.tool_starts[0].name == "echo_tool"
    assert len(surface.tool_outputs) == 1
    assert len(surface.tool_ends) == 1
    assert surface.tool_ends[0].status == "done"
    assert surface.assistant_done[-1] == "done"


def test_rich_surface_approval_supports_view_and_allow_for_session(monkeypatch) -> None:
    answers = iter(["4", "2"])
    monkeypatch.setattr(
        "alysis_code.surface.rich_surface.Prompt.ask",
        lambda *_args, **_kwargs: next(answers),
    )
    surface = RichSurface(console=Console(file=io.StringIO(), force_terminal=False))

    request = ApprovalRequest(
        kind="fs_write",
        reason="review mode",
        preview="diff --git a/a.txt b/a.txt",
        files=["a.txt"],
        allow_for_session_scope=exact_file_set_scope(["a.txt"], operation="fs_write"),
    )
    first = surface.request_approval(request)
    assert first.allow is True
    assert first.allow_for_session is True

    second = surface.request_approval(request)
    assert second.allow is True
    assert second.allow_for_session is True


def test_rich_surface_approval_accepts_legacy_alias_choice(monkeypatch) -> None:
    monkeypatch.setattr(
        "alysis_code.surface.rich_surface.Prompt.ask",
        lambda *_args, **_kwargs: "y",
    )
    surface = RichSurface(console=Console(file=io.StringIO(), force_terminal=False))

    request = ApprovalRequest(
        kind="shell_run",
        reason="review mode",
        preview="pytest -q",
        files=[],
        command="pytest -q",
    )
    decision = surface.request_approval(request)
    assert decision.allow is True
    assert decision.allow_for_session is False


def test_rich_surface_approval_marks_interactive_prompt_active(monkeypatch) -> None:
    observed_states: list[bool] = []

    def _fake_prompt(*_args: object, **_kwargs: object) -> str:
        observed_states.append(is_interactive_prompt_active())
        return "y"

    monkeypatch.setattr(
        "alysis_code.surface.rich_surface.Prompt.ask",
        _fake_prompt,
    )
    surface = RichSurface(console=Console(file=io.StringIO(), force_terminal=False))

    request = ApprovalRequest(
        kind="shell_run",
        reason="review mode",
        preview="pytest -q",
        files=[],
        command="pytest -q",
    )
    decision = surface.request_approval(request)

    assert decision.allow is True
    assert observed_states == [True]
    assert is_interactive_prompt_active() is False


def test_rich_surface_approval_panel_hides_alias_line(monkeypatch) -> None:
    buffer = io.StringIO()
    monkeypatch.setattr(
        "alysis_code.surface.rich_surface.Prompt.ask",
        lambda *_args, **_kwargs: "3",
    )
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))

    request = ApprovalRequest(
        kind="fs_write",
        reason="review mode",
        preview="diff --git a/a.txt b/a.txt\n@@ -0,0 +1 @@\n+x",
        files=["a.txt"],
    )
    decision = surface.request_approval(request)

    out = buffer.getvalue()
    assert decision.allow is False
    assert "Aliases: y/a/n/v also work" not in out
    assert "Approval Required" not in out
    assert "Context available" in out
    assert "[v] to view" in out
    assert "[n] deny" in out


def test_rich_surface_skips_empty_assistant_message_panel() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))

    surface.on_assistant_message_done("")

    assert "(empty response)" not in buffer.getvalue()


def test_rich_surface_renders_warning_without_error_treatment() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))

    surface.on_warning("Model metadata warning for unknown-model-xyz")

    out = buffer.getvalue()
    assert "Warning: Model metadata warning for unknown-model-xyz" in out
    assert "Check base URL, API key, and network connectivity." not in out


def test_rich_surface_renders_tool_transcript_error_guidance() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))

    surface.on_error(
        "LLM error 400: invalid_request_error: "
        "An assistant message with 'tool_calls' must be followed by tool messages "
        "responding to each 'tool_call_id'. The following tool_call_ids did not have "
        "response messages: call_xyz"
    )

    out = " ".join(buffer.getvalue().split())
    assert "did not have response messages: call_xyz" in out
    assert "This session's tool transcript is incomplete or malformed." in out
    assert (
        "Retry in a new session. If it repeats, inspect resume/compaction around missing tool responses."
        in out
    )
    assert "Check base URL, API key, and network connectivity." not in out


def test_recording_surface_falls_back_to_python_warning_for_noop_delegate() -> None:
    delegate = NoopSurface()
    error_calls: list[str] = []
    delegate.on_error = lambda err: error_calls.append(err)  # type: ignore[method-assign]
    surface = RecordingSurface(delegate)

    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        surface.on_warning("Model metadata warning for unknown-model-xyz")

    assert error_calls == []
    assert any("unknown-model-xyz" in str(item.message) for item in seen)


def test_recording_surface_forwards_delegate_warning_without_duplicate_fallback() -> None:
    class _WarningDelegate(NoopSurface):
        def __init__(self) -> None:
            self.warnings: list[str] = []
            self.errors: list[str] = []

        def on_warning(self, warning: str) -> None:
            self.warnings.append(warning)

        def on_error(self, err: str) -> None:
            self.errors.append(err)

    delegate = _WarningDelegate()
    surface = RecordingSurface(delegate)

    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        surface.on_warning("Model metadata warning for unknown-model-xyz")

    assert delegate.warnings == ["Model metadata warning for unknown-model-xyz"]
    assert delegate.errors == []
    assert seen == []


def test_rich_surface_can_hide_status_line() -> None:
    buffer = io.StringIO()
    surface = RichSurface(
        console=Console(file=buffer, force_terminal=False),
        show_status_line=False,
    )

    surface.on_status_update(
        StatusEvent(
            mode="review",
            model="gpt-4.1-mini",
            workspace="/tmp/alysis",
            session_id="sid",
            branch="main",
            dirty=True,
            stream=True,
            task="-",
        )
    )

    assert "mode=review" not in buffer.getvalue()


def test_rich_surface_summarizes_fs_read_without_raw_content_dump() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))

    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call_1",
            name="fs_read",
            args={"path": "README.md"},
            step=1,
        )
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id="call_1",
            name="fs_read",
            chunk=json.dumps(
                {"path": "README.md", "content": "x" * 600, "truncated": True},
                ensure_ascii=True,
            ),
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="call_1",
            name="fs_read",
            status="done",
            elapsed_ms=9,
            meta={},
        )
    )

    out = buffer.getvalue()
    assert "Step 1: Read File" in out
    assert 'Loaded "README.md" (600 chars, truncated).' in out
    assert "(9ms)" in out
    assert "I am reading" not in out
    assert '"content"' not in out


def test_rich_surface_shows_fs_read_window_trace_and_summary() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("full")

    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call_lines",
            name="fs_read",
            args={"path": "README.md", "start_line": 40, "end_line": 42, "max_lines": 10},
            step=2,
        )
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id="call_lines",
            name="fs_read",
            chunk=json.dumps(
                {
                    "path": "README.md",
                    "start_line": 40,
                    "end_line": 42,
                    "total_lines": None,
                    "content": "40: a\n41: b\n42: c\n",
                    "truncated": False,
                },
                ensure_ascii=True,
            ),
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="call_lines",
            name="fs_read",
            status="done",
            elapsed_ms=7,
            meta={},
        )
    )

    out = buffer.getvalue()
    assert "Step 2: Read File" in out
    assert "Input: README.md:40-42 (max 10)" in out
    assert 'Read File: Loaded "README.md" lines 40-42 (3 lines).' in out
    assert "(7ms)" in out


def test_rich_surface_shows_fs_edit_trace_and_summary() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("full")

    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call_edit",
            name="fs_edit",
            args={"path": "src/app.py", "edits": [{"op": "replace_exact"}]},
            step=3,
        )
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id="call_edit",
            name="fs_edit",
            chunk=json.dumps(
                {
                    "path": "src/app.py",
                    "applied_edits": 2,
                    "changed": True,
                    "bytes": 128,
                },
                ensure_ascii=True,
            ),
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="call_edit",
            name="fs_edit",
            status="done",
            elapsed_ms=8,
            meta={},
        )
    )

    out = buffer.getvalue()
    assert "Step 3: Edit File" in out
    assert "Input: src/app.py" in out
    assert 'Edit File: Edited "src/app.py" (2 edit(s), 128 bytes).' in out
    assert "(8ms)" in out


@pytest.mark.parametrize(
    (
        "tool_name",
        "args",
        "result",
        "step",
        "expected_title",
        "expected_input",
        "expected_summary",
    ),
    [
        (
            "fs_move",
            {"source_path": "src/old.py", "destination_path": "src/new.py"},
            {
                "source_path": "src/old.py",
                "destination_path": "src/new.py",
                "moved": True,
                "overwritten": False,
                "bytes": 64,
            },
            4,
            "Move File",
            "Input: src/old.py -> src/new.py",
            'Move File: Moved "src/old.py" -> "src/new.py" (64 bytes).',
        ),
        (
            "fs_copy",
            {"source_path": "src/old.py", "destination_path": "src/new.py"},
            {
                "source_path": "src/old.py",
                "destination_path": "src/new.py",
                "copied": True,
                "overwritten": False,
                "bytes": 64,
            },
            5,
            "Copy File",
            "Input: src/old.py -> src/new.py",
            'Copy File: Copied "src/old.py" -> "src/new.py" (64 bytes).',
        ),
        (
            "fs_delete",
            {"path": "src/old.py"},
            {
                "path": "src/old.py",
                "deleted": True,
                "bytes": 64,
            },
            6,
            "Delete File",
            "Input: src/old.py",
            'Delete File: Deleted "src/old.py" (64 bytes).',
        ),
    ],
)
def test_rich_surface_shows_file_op_trace_and_summary(
    tool_name: str,
    args: dict[str, object],
    result: dict[str, object],
    step: int,
    expected_title: str,
    expected_input: str,
    expected_summary: str,
) -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("full")

    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id=f"call_{tool_name}",
            name=tool_name,
            args=args,
            step=step,
        )
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id=f"call_{tool_name}",
            name=tool_name,
            chunk=json.dumps(result, ensure_ascii=True),
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id=f"call_{tool_name}",
            name=tool_name,
            status="done",
            elapsed_ms=10,
            meta={},
        )
    )

    out = buffer.getvalue()
    assert f"Step {step}: {expected_title}" in out
    assert expected_input in out
    assert expected_summary in out
    assert "(10ms)" in out


def _captured_rich_tool_end(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str,
    meta: dict[str, Any],
    trace_level: str = "compact",
) -> list[tuple[str, str]]:
    surface = RichSurface(console=Console(file=io.StringIO(), force_terminal=False))
    surface.set_trace_level(trace_level)
    lines: list[tuple[str, str]] = []
    monkeypatch.setattr(
        surface,
        "_emit_thinking",
        lambda message, *, style="dim": lines.append((message, style)),
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="fetch-1", name="web_fetch", status=status, elapsed_ms=900, meta=meta
        )
    )
    return lines


def test_rich_surface_renders_remote_site_outcome_as_ordinary_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = _captured_rich_tool_end(
        monkeypatch,
        status="failed",
        meta={
            "error": "HTTP error 429 while fetching '…': remote site is rate-limiting requests.",
            "blocked_by_remote_site": True,
            "remote_site_reason": "site is rate-limiting requests",
        },
    )
    [(message, style)] = lines
    assert "site is rate-limiting requests" in message
    assert "failed" not in message
    assert "HTTP error" not in message
    assert style == rich_surface_mod._STYLE_CHROME
    assert (
        _captured_rich_tool_end(
            monkeypatch,
            status="failed",
            meta={"remote_site_reason": "site didn't respond"},
            trace_level="off",
        )
        == []
    )


def test_rich_surface_marks_withdrawn_tool_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = _captured_rich_tool_end(
        monkeypatch,
        status="done",
        meta={"tool_unavailable": True, "unavailable_reason": "Network is unreachable"},
    )
    [(message, style)] = lines
    assert "unavailable" in message
    assert "Network is unreachable" in message
    assert style == rich_surface_mod._STYLE_WARNING


def test_rich_surface_keeps_red_failure_for_ordinary_fetch_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = _captured_rich_tool_end(
        monkeypatch,
        status="failed",
        meta={"error": "HTTP error 404 while fetching 'https://docs.example.com/missing'."},
    )
    [(message, style)] = lines
    assert "failed" in message
    assert "HTTP error 404" in message
    assert style == "red"


def test_rich_surface_shows_verify_run_trace_and_summary() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("full")

    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call_verify",
            name="verify_run",
            args={"commands": ["pytest -q", "ruff check ."]},
            step=4,
        )
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id="call_verify",
            name="verify_run",
            chunk=json.dumps(
                {
                    "commands": ["pytest -q", "ruff check ."],
                    "command_results": [],
                    "all_passed": False,
                    "failed_commands": ["ruff check ."],
                    "summary": "verification failed (1/2); failed: ruff check .",
                    "primary_failure": {
                        "command": "ruff check .",
                        "effective_command": "ruff check .",
                        "snippet": "F401 imported but unused: requests",
                        "output_truncated": False,
                        "fallback_used": False,
                    },
                    "artifact_path": ".alysis/sessions/s1/verify/step001_verify_run.txt",
                },
                ensure_ascii=True,
            ),
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="call_verify",
            name="verify_run",
            status="done",
            elapsed_ms=12,
            meta={},
        )
    )

    out = buffer.getvalue()
    assert "Step 4: Run Verification" in out
    assert "Input: pytest -q (+1 more)" in out
    assert "Run Verification: verification failed (1/2); failed: ruff check ." in out
    assert "Hint: F401" in out
    assert "imported but unused: requests" in out
    assert "Artifact:" in out
    assert "step001_verify_run.txt" in out
    assert "(12ms)" in out


def test_rich_surface_does_not_imply_external_verify_artifact_is_fs_readable() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("full")

    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call_verify_external",
            name="verify_run",
            args={"commands": ["pytest -q"]},
            step=5,
        )
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id="call_verify_external",
            name="verify_run",
            chunk=json.dumps(
                {
                    "commands": ["pytest -q"],
                    "command_results": [],
                    "all_passed": False,
                    "failed_commands": ["pytest -q"],
                    "summary": "verification failed (0/1); failed: pytest -q",
                    "artifact_path": None,
                    "artifact_saved": True,
                    "artifact_readable_via_fs": False,
                    "artifact_location": "external_session_store",
                },
                ensure_ascii=True,
            ),
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="call_verify_external",
            name="verify_run",
            status="done",
            elapsed_ms=15,
            meta={},
        )
    )

    out = buffer.getvalue()
    assert "Run Verification: verification failed (0/1); failed: pytest -q" in out
    assert "Artifact saved" in out
    assert "externally" in out
    assert "not readable via fs" in out
    assert "Artifact:" not in out
    assert "(15ms)" in out


def test_rich_surface_shows_web_fetch_trace_and_summary() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("full")

    url = "https://docs.python.org/3/library/pathlib.html"
    content = "x" * 137
    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call_web_fetch",
            name="web_fetch",
            args={"url": url, "max_chars": 1200},
            step=6,
        )
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id="call_web_fetch",
            name="web_fetch",
            chunk=json.dumps(
                {
                    "url": url,
                    "final_url": url,
                    "status_code": 200,
                    "content_type": "text/html; charset=utf-8",
                    "content": content,
                    "truncated": True,
                },
                ensure_ascii=True,
            ),
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="call_web_fetch",
            name="web_fetch",
            status="done",
            elapsed_ms=11,
            meta={},
        )
    )

    out = buffer.getvalue()
    assert "Step 6: Fetch Web Page" in out
    assert "Input: https://docs.python.org/3/library/pathlib.html (max_chars=1200)" in out
    assert "Fetch Web Page: Fetched https://docs.python.org/3/library/pathlib.html" in out
    assert "status=200 type=text/html; charset=utf-8" in out
    assert "content=137 chars, truncated." in out
    assert "(11ms)" in out


@pytest.mark.parametrize(
    ("args", "result", "step", "expected_input", "expected_summary"),
    [
        (
            {"mode": "log", "ref": "HEAD", "path": "src/app.py", "grep": "parser"},
            {
                "mode": "log",
                "path": "src/app.py",
                "limit": 10,
                "ref": "HEAD",
                "grep": "parser",
                "author": None,
                "commits": [{"commit": "abc", "subject": "fix parser"}],
                "truncated": False,
            },
            7,
            "Input: log HEAD -- src/app.py grep=parser",
            "Git History: Loaded git history (1 commit(s)).",
        ),
        (
            {"mode": "show", "commit": "abc1234", "path": "src/app.py"},
            {
                "mode": "show",
                "path": "src/app.py",
                "commit": {"short_commit": "abc1234", "subject": "fix parser"},
                "patch_excerpt": "diff --git a/src/app.py b/src/app.py",
                "patch_truncated": True,
            },
            8,
            "Input: show abc1234 -- src/app.py",
            "Git History: Loaded commit abc1234 (36 chars, truncated).",
        ),
        (
            {"mode": "blame", "path": "src/app.py", "start_line": 10, "end_line": 12},
            {
                "mode": "blame",
                "path": "src/app.py",
                "start_line": 10,
                "end_line": 12,
                "lines": [{}, {}, {}],
                "truncated": False,
            },
            9,
            "Input: blame src/app.py:10-12",
            'Git History: Loaded blame for "src/app.py" lines 10-12 (3 line(s)).',
        ),
    ],
)
def test_rich_surface_shows_git_history_trace_and_summary(
    args: dict[str, object],
    result: dict[str, object],
    step: int,
    expected_input: str,
    expected_summary: str,
) -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("full")

    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id=f"call_git_history_{step}",
            name="git_history",
            args=args,
            step=step,
        )
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id=f"call_git_history_{step}",
            name="git_history",
            chunk=json.dumps(result, ensure_ascii=True),
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id=f"call_git_history_{step}",
            name="git_history",
            status="done",
            elapsed_ms=14,
            meta={},
        )
    )

    out = buffer.getvalue()
    assert f"Step {step}: Git History" in out
    assert expected_input in out
    assert expected_summary in out
    assert "(14ms)" in out


def test_rich_surface_shows_symbol_search_trace_and_summary() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("full")

    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call_symbol_search",
            name="symbol_search",
            args={"query": "build_tools", "kind": "function", "exact": True},
            step=10,
        )
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id="call_symbol_search",
            name="symbol_search",
            chunk=json.dumps(
                {
                    "query": "build_tools",
                    "kind": "function",
                    "root_path": ".",
                    "exact": True,
                    "matches": [
                        {
                            "path": "src/alysis_code/agent_loop.py",
                            "line": 944,
                            "kind": "function",
                            "name": "build_tools",
                            "signature": "def build_tools(...)",
                        }
                    ],
                    "truncated": False,
                    "notes": [],
                    "backend": "python_ast",
                    "parsed_files": 5,
                },
                ensure_ascii=True,
            ),
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="call_symbol_search",
            name="symbol_search",
            status="done",
            elapsed_ms=11,
            meta={},
        )
    )

    out = buffer.getvalue()
    assert "Step 10: Symbol Search" in out
    assert "Input: build_tools kind=function exact" in out
    assert 'Symbol Search: Found 1 symbol match(es) for "build_tools".' in out
    assert "(11ms)" in out


def test_rich_surface_shows_history_search_trace_and_summary() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("full")

    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call_history_search",
            name="history_search",
            args={"pattern": "verify_run"},
            step=11,
        )
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id="call_history_search",
            name="history_search",
            chunk=json.dumps(
                {
                    "pattern": "verify_run",
                    "matches": [
                        {
                            "kind": "history",
                            "path": ".alysis/sessions/s1/history/chunk_0001.jsonl",
                            "line": 10,
                            "text": "verify_run called",
                        }
                    ],
                    "truncated": False,
                },
                ensure_ascii=True,
            ),
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="call_history_search",
            name="history_search",
            status="done",
            elapsed_ms=13,
            meta={},
        )
    )

    out = buffer.getvalue()
    assert "Step 11: Search Session History" in out
    assert "Input: verify_run" in out
    assert 'Search Session History: Found 1 history match(es) for "verify_run".' in out
    assert "(13ms)" in out


def test_rich_surface_summarizes_subagent_run_output() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("full")

    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call_sub",
            name="subagent_run",
            args={"name": "explorer", "task": "map structure"},
            step=3,
        )
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id="call_sub",
            name="subagent_run",
            chunk=json.dumps(
                {
                    "subagent": "explorer",
                    "result": "abc",
                    "sandbox": {"mode": "readonly", "tools": ["fs_read", "search_rg"]},
                },
                ensure_ascii=True,
            ),
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="call_sub",
            name="subagent_run",
            status="done",
            elapsed_ms=11,
            meta={},
        )
    )

    out = buffer.getvalue()
    assert "Step 3: Run Subagent" in out
    assert 'Subagent "explorer" mode=readonly (tools=2, result=3 chars).' in out


def test_rich_surface_progress_update_deduplicates_repeated_lines() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))

    surface.on_progress_update("Planning next step.")
    surface.on_progress_update("Planning next step.")

    out = buffer.getvalue()
    assert out.count("Planning next step.") == 1


def test_rich_surface_uses_permissions_label_for_execution_mode_changes() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))

    surface.emit_mode_changed("auto")

    out = buffer.getvalue()
    assert "Permissions: auto" in out
    assert "Mode: auto" not in out


@pytest.mark.parametrize(
    "message",
    [
        "Applied planner update to the Forge plan.",
        "Planner response ready.",
        "Planner update was a no-op.",
        "Planner returned an error; using fallback handling.",
        "Planner request recovered after 1 transient retry.",
        "Swarm completed with exit code 1. Summary: summary.md.",
        "[None] Swarm completed with exit code 1. Summary: summary.md.",
        "[T01] Swarm aborted: worker failed before completion.",
    ],
)
def test_rich_surface_terminal_progress_does_not_restart_spinner(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    started: list[str] = []
    stopped: list[None] = []
    monkeypatch.setattr(
        surface,
        "_start_thinking_spinner",
        lambda *, label: started.append(label),
    )
    monkeypatch.setattr(
        surface,
        "_stop_thinking_spinner",
        lambda: stopped.append(None),
    )

    surface.on_progress_update("Receiving planner output...")
    assert started == ["Thinking..."]

    started.clear()
    stopped.clear()
    surface.on_progress_update(message)

    assert started == []
    assert stopped
    assert message in buffer.getvalue()


def test_rich_surface_trace_off_hides_reasoning_lines() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("off")

    surface.on_progress_update("Understanding your request.")
    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call_1",
            name="fs_read",
            args={"path": "README.md"},
            step=1,
        )
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id="call_1",
            name="fs_read",
            chunk=json.dumps({"path": "README.md", "content": "abc"}, ensure_ascii=True),
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="call_1",
            name="fs_read",
            status="done",
            elapsed_ms=5,
            meta={},
        )
    )

    out = buffer.getvalue()
    assert "Thinking" not in out
    assert "Step 1" not in out
    assert "Read File" not in out


def test_rich_surface_compact_trace_flushes_safe_reasoning_summary_before_answer() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False, width=240))

    assert surface.reasoning_trace_enabled is True
    surface.on_reasoning_token("Compare the available options.\n")
    surface.on_reasoning_token("Choose the smallest safe change.")
    surface.on_assistant_message_done("Done.")

    out = buffer.getvalue()
    assert (
        "Reasoning summary: Compare the available options. Choose the smallest safe change." in out
    )
    assert "Done." in out
    assert out.find("Reasoning summary:") < out.find("Done.")
    assert "Thinking summary" not in out


def test_rich_surface_streams_safe_reasoning_summary_live_on_terminal() -> None:
    buffer = io.StringIO()
    surface = RichSurface(
        console=Console(
            file=buffer,
            force_terminal=True,
            color_system=None,
            width=240,
        )
    )

    surface.on_reasoning_token("Live safe summary.")

    assert surface._reasoning_summary_live is not None
    assert "Live safe summary." in surface._reasoning_summary_renderable().plain
    surface.on_assistant_message_done("Done.")


def test_rich_surface_compact_trace_redacts_and_truncates_reasoning_summary() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False, width=240))
    tail = "tail-marker-should-not-be-rendered"

    surface.on_reasoning_token(f"Bearer super-secret-token {'x' * 220} {tail}")
    surface.on_warning("fallback warning")

    out = buffer.getvalue()
    assert "Bearer [REDACTED]" in out
    assert "super-secret-token" not in out
    assert tail not in out
    assert "..." in out
    assert out.find("Reasoning summary:") < out.find("fallback warning")


def test_rich_surface_full_trace_expands_reasoning_summary_before_tool() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("full")

    surface.on_reasoning_token("Inspect the protocol contract.\nPreserve provider defaults.")
    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call_summary",
            name="fs_read",
            args={"path": "README.md"},
            step=1,
        )
    )

    out = buffer.getvalue()
    assert "Reasoning summary:" in out
    assert "Inspect the protocol contract." in out
    assert "Preserve provider defaults." in out
    assert out.find("Reasoning summary:") < out.find("Step 1: Read File")


def test_rich_surface_reasoning_lifecycle_separates_provider_calls() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False, width=240))

    surface.on_reasoning_start("call-1")
    surface.on_reasoning_token("First safe summary.")
    surface.on_reasoning_start("call-2")
    surface.on_reasoning_token("Second safe summary.")
    surface.on_reasoning_end("call-2")

    out = buffer.getvalue()
    assert out.count("Reasoning summary:") == 2
    assert out.find("First safe summary.") < out.find("Second safe summary.")
    assert surface._reasoning_summary_block_id is None


def test_rich_surface_reasoning_lifecycle_ignores_stale_end() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False, width=240))

    surface.on_reasoning_start("current")
    surface.on_reasoning_token("Keep this live.")
    surface.on_reasoning_end("stale")

    assert buffer.getvalue() == ""
    assert surface._reasoning_summary_block_id == "current"
    surface.on_reasoning_end("current")
    assert "Keep this live." in buffer.getvalue()


def test_rich_surface_trace_off_suppresses_and_clears_reasoning_summary() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))

    surface.on_reasoning_token("This pending summary must be discarded.")
    surface.set_trace_level("off")
    assert surface.reasoning_trace_enabled is False
    surface.on_reasoning_token("This disabled summary must be ignored.")
    surface.on_assistant_message_done("Done.")

    out = buffer.getvalue()
    assert "Reasoning summary" not in out
    assert "pending summary" not in out
    assert "disabled summary" not in out
    assert "Done." in out


def test_rich_surface_trace_off_still_reports_tool_failures() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("off")

    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call_failed",
            name="fs_read",
            args={"path": "missing.txt"},
            step=1,
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="call_failed",
            name="fs_read",
            status="failed",
            elapsed_ms=5,
            meta={"error": "file not found"},
        )
    )

    out = buffer.getvalue()
    assert "Read File failed" in out
    assert "file not found" in out


def test_rich_surface_trace_full_emits_only_observed_tool_details() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("full")

    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call_2",
            name="search_rg",
            args={"pattern": "main\\("},
            step=2,
        )
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id="call_2",
            name="search_rg",
            chunk=json.dumps({"pattern": "main\\(", "matches": []}, ensure_ascii=True),
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="call_2",
            name="search_rg",
            status="done",
            elapsed_ms=14,
            meta={},
        )
    )

    out = buffer.getvalue()
    assert "Step 2: Search Workspace" in out
    assert "Input: main\\(" in out
    assert "Search Workspace" in out
    assert "(14ms)" in out
    assert "Goal:" not in out
    assert "Action:" not in out
    assert "Fallback:" not in out
    assert "Decision:" not in out


def test_rich_surface_visually_separates_thinking_and_answer() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))

    surface.on_progress_update("Understanding your request.")
    surface.on_assistant_message_done("Here is the final answer.")

    out = buffer.getvalue()
    assert "Understanding your request." in out
    assert "Here is the final answer." in out
    assert "Agent Response" not in out
    assert out.find("Understanding your request.") < out.find("Here is the final answer.")


def test_rich_surface_shows_generic_thinking_indicator_for_trace_off_text_turn() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("off")

    surface.on_user_message("hello")
    surface.on_assistant_message_done("Here is the final answer.")

    out = buffer.getvalue()
    assert "hello" in out
    assert "Thinking..." in out
    assert "Here is the final answer." in out
    assert out.find("hello") < out.find("Thinking...") < out.find("Here is the final answer.")


def test_format_turn_elapsed_compacts_seconds_and_minutes() -> None:
    assert rich_surface_mod._format_turn_elapsed(0.9) is None
    assert rich_surface_mod._format_turn_elapsed(1.0) == "1s"
    assert rich_surface_mod._format_turn_elapsed(4.8) == "4s"
    assert rich_surface_mod._format_turn_elapsed(60.0) == "1m"
    assert rich_surface_mod._format_turn_elapsed(65.2) == "1m 5s"


def test_rich_surface_shows_elapsed_time_in_answer_separator(monkeypatch) -> None:
    monotonic_values = iter([100.0, 104.8])
    monkeypatch.setattr(
        "alysis_code.surface.rich_surface.time.monotonic",
        lambda: next(monotonic_values),
    )
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("off")

    surface.on_user_message("hello")
    surface.on_assistant_message_done("Here is the final answer.")

    out = buffer.getvalue()
    assert "4s" in out
    assert out.find("Thinking...") < out.find("4s") < out.find("Here is the final answer.")


def test_rich_surface_does_not_show_generic_thinking_indicator_when_progress_visible() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))

    surface.on_user_message("hello")
    surface.on_progress_update("Understanding your request.")
    surface.on_assistant_message_done("Here is the final answer.")

    out = buffer.getvalue()
    assert "hello" in out
    assert "Understanding your request." in out
    assert "Thinking..." not in out


def test_rich_surface_skips_rerender_for_streamed_plain_text(monkeypatch) -> None:
    text = (
        "Now let me verify the implementation by running the news aggregator and "
        "checking that everything works:"
    )
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=True, width=120))
    erase_calls: list[int] = []
    render_calls: list[str] = []

    monkeypatch.setattr(
        surface, "_erase_streamed_output_lines", lambda count: erase_calls.append(count)
    )
    monkeypatch.setattr(surface, "_render_markdown", lambda value: render_calls.append(value))

    surface.on_assistant_token(text)
    surface.on_assistant_message_done(text)

    assert erase_calls == []
    assert render_calls == []
    assert buffer.getvalue().count(text) == 1


def test_rich_surface_skips_rerender_for_streamed_inline_markdown_prose(monkeypatch) -> None:
    text = "Now let me update the `contents.md` to include the AI news section:"
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=True, width=120))
    erase_calls: list[int] = []
    render_calls: list[str] = []

    monkeypatch.setattr(
        surface, "_erase_streamed_output_lines", lambda count: erase_calls.append(count)
    )
    monkeypatch.setattr(surface, "_render_markdown", lambda value: render_calls.append(value))

    surface.on_assistant_token(text)
    surface.on_assistant_message_done(text)

    assert erase_calls == []
    assert render_calls == []
    assert buffer.getvalue().count(text) == 1


def test_rich_surface_rerenders_streamed_markdown(monkeypatch) -> None:
    text = "- First item\n- Second item"
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=True, width=120))
    erase_calls: list[int] = []
    render_calls: list[str] = []

    monkeypatch.setattr(
        surface, "_erase_streamed_output_lines", lambda count: erase_calls.append(count)
    )
    monkeypatch.setattr(surface, "_render_markdown", lambda value: render_calls.append(value))

    surface.on_assistant_token(text)
    surface.on_assistant_message_done(text)

    assert len(erase_calls) == 1
    assert render_calls == [text]


def test_rich_surface_shows_working_banner_after_tool_start_in_active_turn() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("off")

    surface.on_user_message("hello")
    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call_working",
            name="fs_read",
            args={"path": "README.md"},
            step=1,
        )
    )

    out = buffer.getvalue()
    assert "Working... Press Esc to interrupt." in out


def test_rich_surface_shows_elapsed_time_on_first_working_banner_only(monkeypatch) -> None:
    monotonic_values = iter([200.0, 203.4])
    monkeypatch.setattr(
        "alysis_code.surface.rich_surface.time.monotonic",
        lambda: next(monotonic_values),
    )
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))

    surface.on_user_message("hello")
    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call_elapsed",
            name="fs_read",
            args={"path": "README.md"},
            step=1,
        )
    )
    surface.on_assistant_message_done("Done.")

    out = buffer.getvalue()
    assert "Working... Press Esc to interrupt." in out
    assert "3s" in out
    assert out.find("Working... Press Esc to interrupt.") < out.find("Step 1: Read File")
    assert out.count("3s") == 1


def test_rich_surface_omits_subsecond_elapsed_labels(monkeypatch) -> None:
    monotonic_values = iter([300.0, 300.4])
    monkeypatch.setattr(
        "alysis_code.surface.rich_surface.time.monotonic",
        lambda: next(monotonic_values),
    )
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("off")

    surface.on_user_message("hello")
    surface.on_assistant_message_done("Done.")

    out = buffer.getvalue()
    assert "0s" not in out
    assert "1s" not in out


def test_rich_surface_does_not_show_working_banner_without_active_turn() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("off")

    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call_no_turn",
            name="fs_read",
            args={"path": "README.md"},
            step=1,
        )
    )

    out = buffer.getvalue()
    assert "Working... Press Esc to interrupt." not in out


def test_rich_surface_compact_trace_summarizes_subagent_run_output() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))

    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call_sub_compact",
            name="subagent_run",
            args={"name": "reviewer", "task": "review diff"},
            step=4,
        )
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id="call_sub_compact",
            name="subagent_run",
            chunk=json.dumps(
                {
                    "subagent": "reviewer",
                    "result": "summary",
                    "sandbox": {"mode": "readonly", "tools": ["fs_read"]},
                },
                ensure_ascii=True,
            ),
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="call_sub_compact",
            name="subagent_run",
            status="done",
            elapsed_ms=7,
            meta={},
        )
    )

    out = buffer.getvalue()
    assert "Step 4: Run Subagent" in out
    assert "reviewer" in out
    assert "mode=readonly" in out


def test_rich_surface_renders_nested_subagent_progress_with_visual_prefix() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))

    surface.on_subagent_start(
        SubagentStartEvent(
            name="explorer",
            mode="readonly",
        )
    )
    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="sub_call_1",
            name="fs_read",
            args={"path": "README.md"},
            step=1,
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id="sub_call_1",
            name="fs_read",
            chunk=json.dumps(
                {"path": "README.md", "content": "x" * 42, "truncated": False},
                ensure_ascii=True,
            ),
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="sub_call_1",
            name="fs_read",
            status="done",
            elapsed_ms=12,
            meta={},
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    surface.on_subagent_end(
        SubagentEndEvent(
            name="explorer",
            mode="readonly",
            status="success",
            elapsed_ms=1234,
            steps_completed=1,
        )
    )

    out = buffer.getvalue()
    assert "╭─ explorer · readonly" in out
    assert "│ Step 1: Read File" in out
    assert '│ Read File: Loaded "README.md" (42 chars).' in out
    assert "╰─ finished · 1 step · 1.2s" in out
    assert "[explorer]" not in out


def test_rich_surface_full_trace_shows_nested_subagent_detail_lines() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("full")

    surface.on_subagent_start(SubagentStartEvent(name="reviewer", mode="readonly"))
    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="sub_call_2",
            name="search_rg",
            args={"pattern": "subagent_run"},
            step=2,
            subagent_name="reviewer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id="sub_call_2",
            name="search_rg",
            chunk=json.dumps({"pattern": "subagent_run", "matches": []}, ensure_ascii=True),
            subagent_name="reviewer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="sub_call_2",
            name="search_rg",
            status="done",
            elapsed_ms=14,
            meta={},
            subagent_name="reviewer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )

    out = buffer.getvalue()
    assert "╭─ reviewer · readonly" in out
    assert "│ Step 2: Search Workspace" in out
    assert "│ Input: subagent_run" in out
    assert "│ Goal:" not in out
    assert "│ Action:" not in out
    assert "│ Fallback:" not in out
    assert "│ Decision:" not in out
    assert "[reviewer]" not in out


def test_rich_surface_trace_off_hides_nested_subagent_progress() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))
    surface.set_trace_level("off")

    surface.on_subagent_start(SubagentStartEvent(name="explorer", mode="readonly"))
    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="sub_call_off",
            name="fs_read",
            args={"path": "README.md"},
            step=1,
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id="sub_call_off",
            name="fs_read",
            chunk=json.dumps({"path": "README.md", "content": "abc"}, ensure_ascii=True),
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="sub_call_off",
            name="fs_read",
            status="done",
            elapsed_ms=5,
            meta={},
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    surface.on_subagent_end(
        SubagentEndEvent(
            name="explorer",
            mode="readonly",
            status="success",
            elapsed_ms=20,
            steps_completed=1,
        )
    )

    out = buffer.getvalue()
    assert "explorer" not in out
    assert "Read File" not in out


def test_rich_surface_renders_failed_subagent_box_with_error_inside_block() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))

    surface.on_subagent_start(SubagentStartEvent(name="custom-audit", mode="readonly"))
    surface.on_subagent_end(
        SubagentEndEvent(
            name="custom-audit",
            mode="readonly",
            status="failed",
            elapsed_ms=35,
            steps_completed=2,
            error="timed out while reading files",
        )
    )

    out = buffer.getvalue()
    assert "╭─ custom-audit · readonly" in out
    assert "│ Error: timed out while reading files" in out
    assert "╰─ failed · 2 steps · 35ms" in out


def test_rich_surface_renders_user_message_with_left_bar() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))

    surface.on_user_message("line one\nline two")

    out = buffer.getvalue()
    assert "line one" in out
    assert "line two" in out
    assert "You" not in out
