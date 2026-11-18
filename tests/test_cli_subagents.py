from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from alysis_code import cli as cli_mod
from alysis_code.config import AppConfig


def _session_with_scheduler(children: list[dict[str, object]]):
    class _Scheduler:
        def __init__(self) -> None:
            self.calls = 0

        def status(self) -> dict[str, object]:
            self.calls += 1
            return {"children": children, "unapplied_isolated_results": []}

    session = type("Session", (), {})()
    session.subagents_enabled = True
    session.cfg = AppConfig(model="test-model")
    session.mode = "review"
    session.tools = {}
    session.subagent_registry = {}
    session.child_scheduler = _Scheduler()
    return session


@pytest.mark.parametrize("role", ["explorer", "general"])
def test_plural_subagents_command_lists_only_active_children_with_state(role: str) -> None:
    session = _session_with_scheduler(
        [
            {
                "run_id": "run-live",
                "subagent": role,
                "state": "running",
                "workspace_view": "shared",
                "elapsed_ms": 1500,
                "steps_completed": 2,
                "activity": "Running readonly investigation.",
            },
            {
                "run_id": "run-done",
                "subagent": "reviewer",
                "state": "joined",
                "workspace_view": "shared",
                "elapsed_ms": 900,
                "steps_completed": 1,
                "activity": "Joined.",
            },
        ]
    )
    stream = io.StringIO()

    result = cli_mod._handle_chat_command(
        input_text="/subagents",
        root=Path("."),
        session=session,
        pending_images=[],
        console=Console(file=stream, force_terminal=False),
    )

    assert result == "handled"
    assert session.child_scheduler.calls == 1
    output = stream.getvalue()
    assert role in output
    assert "running" in output
    assert "reviewer" not in output


def test_plural_subagents_command_handles_empty_active_set() -> None:
    session = _session_with_scheduler([])
    stream = io.StringIO()

    result = cli_mod._handle_chat_command(
        input_text="/subagents",
        root=Path("."),
        session=session,
        pending_images=[],
        console=Console(file=stream, force_terminal=False),
    )

    assert result == "handled"
    assert "no active subagents" in stream.getvalue().lower()


@pytest.mark.parametrize(
    ("command", "reported_command"),
    [
        ("/subagent", "/subagent"),
        ("/subagent explorer inspect auth", "/subagent"),
        ("/subagent on", "/subagent"),
        ("/subagent off", "/subagent"),
        ("/subagent status", "/subagent"),
        ("/subagent view run-1", "/subagent"),
        ("/agents", "/agents"),
        ("/explorer", "/explorer"),
        ("/explore", "/explore"),
        ("/review", "/review"),
        ("/reviewer", "/reviewer"),
        ("/tests", "/tests"),
        ("/test-strategist", "/test-strategist"),
        ("/engineer", "/engineer"),
        ("/agent explorer inspect auth boundaries", "/agent"),
    ],
)
def test_removed_subagent_commands_fall_through_to_unknown_command(
    command: str,
    reported_command: str,
) -> None:
    session = type("Session", (), {})()
    session.subagents_enabled = False
    session.cfg = AppConfig(model="test-model")
    session.mode = "review"
    session.tools = {}
    session.subagent_registry = {}

    stream = io.StringIO()
    result = cli_mod._handle_chat_command(
        input_text=command,
        root=Path("."),
        session=session,
        pending_images=[],
        console=Console(file=stream, force_terminal=False),
    )

    assert result == "handled"
    output = stream.getvalue()
    assert f"Unknown command: {reported_command}." in output
    assert "Try /help." in output
