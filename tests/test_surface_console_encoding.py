from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from rich.text import Text

from alysis_code.surface.console import (
    make_console,
    safe_plain_error,
    stream_needs_ascii_fallback,
)
from alysis_code.surface.rich_surface import RichSurface
from alysis_code.surface.types import (
    ApprovalRequest,
    PatchEvent,
    StatusEvent,
    SubagentEndEvent,
    SubagentStartEvent,
)


class _EncodedTextStream:
    def __init__(self, *, encoding: str, tty: bool = False) -> None:
        self.encoding = encoding
        self._tty = tty
        self._parts: list[str] = []

    def write(self, text: str) -> int:
        text.encode(self.encoding)
        self._parts.append(text)
        return len(text)

    def flush(self) -> None:
        return

    def isatty(self) -> bool:
        return self._tty

    def getvalue(self) -> str:
        return "".join(self._parts)


class _BrokenConsole:
    def __init__(self, stream: _EncodedTextStream) -> None:
        self.file = stream
        self.is_terminal = False
        self.width = 80

    def print(self, *_args: Any, **_kwargs: Any) -> None:
        raise UnicodeEncodeError("charmap", "│", 0, 1, "character maps to undefined")


def _assert_ascii_safe(text: str) -> None:
    text.encode("ascii")
    assert "│" not in text
    assert "╭" not in text
    assert "╰" not in text
    assert "─" not in text
    assert "•" not in text
    assert "·" not in text


def test_make_console_cp1252_prints_error_renderable_without_unicode_error() -> None:
    stream = _EncodedTextStream(encoding="cp1252", tty=True)
    console = make_console(file=stream, force_terminal=True, width=80)

    console.print(Text.assemble(("│ ", "red"), ("boom sk-testsecret1234567890", "red")))

    rendered = stream.getvalue()
    _assert_ascii_safe(rendered)
    assert "| boom" in rendered
    assert "sk-testsecret" not in rendered
    assert "[REDACTED]" in rendered


def test_make_console_utf8_keeps_rich_unicode_when_stream_supports_it() -> None:
    stream = _EncodedTextStream(encoding="utf-8", tty=True)
    console = make_console(file=stream, force_terminal=True, width=80)

    console.print(Text("│ ok"))

    assert "│ ok" in stream.getvalue()


def test_stream_needs_ascii_fallback_for_windows_default_encoding() -> None:
    assert stream_needs_ascii_fallback(_EncodedTextStream(encoding="cp1252")) is True
    assert stream_needs_ascii_fallback(_EncodedTextStream(encoding="utf-8")) is False


def test_rich_surface_error_is_ascii_safe_on_cp1252_stream() -> None:
    stream = _EncodedTextStream(encoding="cp1252", tty=True)
    surface = RichSurface(console=make_console(file=stream, force_terminal=True, width=100))

    surface.on_error("failure │ sk-testsecret1234567890")

    rendered = stream.getvalue()
    _assert_ascii_safe(rendered)
    assert "failure |" in rendered
    assert "sk-testsecret" not in rendered
    assert "[REDACTED]" in rendered


def test_rich_surface_warning_and_status_render_ascii_safe_on_cp1252_stream() -> None:
    stream = _EncodedTextStream(encoding="cp1252", tty=True)
    surface = RichSurface(console=make_console(file=stream, force_terminal=True, width=100))

    surface.on_status_update(
        StatusEvent(
            mode="review",
            model="model",
            workspace="C:/repo",
            session_id="s1",
            branch="main",
            dirty=True,
            stream=True,
            task="task",
        )
    )
    surface.on_warning("warn │ sk-testsecret1234567890")

    rendered = stream.getvalue()
    _assert_ascii_safe(rendered)
    assert "repo - model - review - task - dirty" in rendered
    assert "Warning:" in rendered
    assert "[REDACTED]" in rendered


def test_rich_surface_tool_and_subagent_renderables_ascii_safe_on_cp1252_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _EncodedTextStream(encoding="cp1252", tty=True)
    surface = RichSurface(console=make_console(file=stream, force_terminal=True, width=120))
    monkeypatch.setattr(
        "alysis_code.surface.rich_surface.Prompt.ask",
        lambda *_args, **_kwargs: "n",
    )
    # Force the line-based fallback path this test stubs: on hosts with a real
    # /dev/tty (e.g. a tmux-run suite) the inline selector would otherwise open
    # the terminal and block forever waiting for a human keypress.
    monkeypatch.setattr(
        "alysis_code.surface.rich_surface.RichSurface._uses_inline_approval_selector",
        lambda self: False,
    )

    surface.on_user_message("please inspect │")
    surface.on_progress_update("Planner update · running │")
    surface.on_subagent_start(SubagentStartEvent(name="reviewer", mode="readonly"))
    surface.on_subagent_end(
        SubagentEndEvent(
            name="reviewer",
            mode="readonly",
            status="success",
            elapsed_ms=12,
            steps_completed=1,
        )
    )
    surface.on_patch_generated(
        PatchEvent(files=["a.py"], diff="diff --git a/a.py b/a.py\n+print('│')", summary="patch │")
    )
    decision = surface.request_approval(
        ApprovalRequest(
            kind="shell_run",
            reason="verify │",
            preview="pytest │",
            files=["a.py"],
            command="python -m pytest",
        )
    )

    assert decision.allow is False
    _assert_ascii_safe(stream.getvalue())


def test_exception_fallback_does_not_double_crash_when_console_print_fails() -> None:
    stream = _EncodedTextStream(encoding="cp1252", tty=True)
    surface = RichSurface(console=_BrokenConsole(stream))  # type: ignore[arg-type]

    surface.on_error("boom │ sk-testsecret1234567890")

    rendered = stream.getvalue()
    _assert_ascii_safe(rendered)
    assert "Alysis Code UnicodeEncodeError: boom |" in rendered
    assert "Tip: enable UTF-8 mode" in rendered
    assert "sk-testsecret" not in rendered


def test_safe_plain_error_stdout_and_stderr_paths_are_ascii_and_redacted() -> None:
    stdout = _EncodedTextStream(encoding="cp1252")
    stderr = _EncodedTextStream(encoding="cp1252")

    safe_plain_error(stream=stdout, error_type="Error │", message="bad │ sk-testsecret1234567890")
    safe_plain_error(
        stream=stderr,
        error_type="RuntimeError",
        message="failed · sk-testsecret1234567890",
    )

    for rendered in (stdout.getvalue(), stderr.getvalue()):
        _assert_ascii_safe(rendered)
        assert "sk-testsecret" not in rendered
        assert "[REDACTED]" in rendered


def test_source_console_construction_uses_safe_factory() -> None:
    offenders: list[str] = []
    source_root = Path("src/alysis_code")
    for path in source_root.rglob("*.py"):
        if path.as_posix().endswith("surface/console.py"):
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"\bConsole\(", text):
            offenders.append(path.as_posix())

    assert offenders == []
