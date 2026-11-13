"""The model chooses how long a command may run.

A fixed 60 second limit stopped every slow build, install or test run. The
model knows what it is starting, so it sets ``timeout_s``; a command that
still reaches its limit says how to continue instead of only failing.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from test_approval_review import _store, _Surface

from alysis_code.agent.tools_assembly import (
    SHELL_RUN_DEFAULT_TIMEOUT_SECONDS,
    SHELL_RUN_MAX_TIMEOUT_SECONDS,
    build_tools,
    shell_run_timeout_guidance,
    shell_run_timeout_seconds,
)
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.sandbox_runner import HostShellRunner
from alysis_code.tools.registry import require_builtin_tool_metadata
from alysis_code.tools.shell import ShellError, shell_run


@pytest.mark.parametrize(
    "requested,expected",
    [
        (None, 60.0),
        (300, 300.0),
        (90.5, 90.5),
        ("120", 120.0),
        (" 45 ", 45.0),
        # Out of range moves to the nearest end; it is never refused.
        (5000, 600.0),
        (600.001, 600.0),
        (0, 1.0),
        (-30, 1.0),
        # Not a usable number: the default.
        ("soon", 60.0),
        ("", 60.0),
        (True, 60.0),
        (float("nan"), 60.0),
        (float("inf"), 60.0),
        ([300], 60.0),
        ({"seconds": 300}, 60.0),
    ],
)
def test_requested_limit_is_kept_inside_the_supported_range(requested, expected) -> None:
    assert shell_run_timeout_seconds(requested) == expected
    assert SHELL_RUN_DEFAULT_TIMEOUT_SECONDS == 60.0 and SHELL_RUN_MAX_TIMEOUT_SECONDS == 600.0


def test_the_model_is_told_about_the_limit() -> None:
    metadata = require_builtin_tool_metadata("shell_run")
    timeout = metadata.parameters["properties"]["timeout_s"]
    assert timeout["type"] == "number"
    assert "600" in timeout["description"] and "shell_background" in timeout["description"]
    # A hard schema bound would refuse a call the host can simply clamp.
    assert "maximum" not in timeout and "minimum" not in timeout
    assert metadata.parameters["required"] == ["cmd"]


@pytest.fixture
def run_tool(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    seen: list[dict] = []

    def capture(**kwargs):
        seen.append(kwargs)
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr("alysis_code.agent_loop.shell_run", capture)
    tools = build_tools(
        root=root,
        console=Console(file=io.StringIO()),
        surface=_Surface(allow=True),
        store=_store(root),
        mode="fullaccess",
        yes=True,
        shell_runner=HostShellRunner(),
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )

    def run(**arguments):
        tools["shell_run"].run({"cmd": "true", **arguments})
        return seen[-1]

    return run, tools


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ({}, 60.0),
        ({"timeout_s": 240}, 240.0),
        ({"timeout_s": 9999}, 600.0),
        ({"timeout_s": "x"}, 60.0),
    ],
)
def test_shell_run_uses_the_limit_the_model_chose(run_tool, arguments, expected) -> None:
    run, tools = run_tool
    call = run(**arguments)
    assert call["timeout_s"] == expected
    assert "timeout_s" in tools["shell_run"].parameters["properties"]
    # The command is told what to say if it reaches that limit.
    assert call["timeout_guidance"] == shell_run_timeout_guidance(expected)


def test_guidance_offers_only_what_is_still_possible() -> None:
    raisable = shell_run_timeout_guidance(60.0)
    assert "larger timeout_s (at most 600)" in raisable and "shell_background" in raisable
    at_limit = shell_run_timeout_guidance(600.0)
    assert "larger timeout_s" not in at_limit and "shell_background" in at_limit


def test_a_command_that_reaches_its_limit_says_how_to_continue(tmp_path) -> None:
    with pytest.raises(ShellError) as stopped:
        shell_run(
            root=tmp_path,
            cmd="sleep 5",
            timeout_s=1,
            runner=HostShellRunner(),
            timeout_guidance=shell_run_timeout_guidance(1.0),
        )
    message = str(stopped.value)
    assert message.startswith("Command timed out after 1s and was stopped.")
    assert "timeout_s" in message and "shell_background" in message
    payload = stopped.value.result_payload
    assert payload["timed_out"] is True and payload["error"] == message


def test_other_callers_keep_the_plain_message(tmp_path) -> None:
    with pytest.raises(ShellError, match=r"^Command timed out after 1s$"):
        shell_run(root=tmp_path, cmd="sleep 5", timeout_s=1, runner=HostShellRunner())
