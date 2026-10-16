from __future__ import annotations

import io
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.cli_impl.chat import commands as chat_commands
from alysis_code.config import AppConfig
from alysis_code.skills import discover_skills
from alysis_code.workspace_context import WorkspaceContext


@pytest.fixture(autouse=True)
def _isolate_cli_workspace_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep CLI fixtures scoped even when pytest's base temp lives in this Git repo."""

    def resolve(path: Path) -> WorkspaceContext:
        root = Path(path).expanduser().resolve()
        return WorkspaceContext(
            input_path=root,
            focus_path=root,
            workspace_root=root,
            git_root=None,
            focus_relpath=".",
            workspace_kind="plain_dir",
            has_head_commit=False,
            current_branch=None,
        )

    monkeypatch.setattr(
        cli_mod,
        "resolve_workspace_context",
        resolve,
    )


def _write_skill(
    root: Path,
    rel_root: str,
    bundle_name: str,
    *,
    name: str,
    description: str,
    body: str,
) -> None:
    bundle = root / rel_root / bundle_name
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "SKILL.md").write_text(
        (f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"),
        encoding="utf-8",
    )


def _write_invalid_utf8_skill(root: Path, rel_root: str, bundle_name: str) -> None:
    bundle = root / rel_root / bundle_name
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "SKILL.md").write_bytes(
        b"---\nname: broken\ndescription: broken\n---\n\n\xff\xfe\xfa\n"
    )


def test_skill_list_cli_lists_discovered_skills_with_source_metadata(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        ".alysis_skills",
        "python",
        name="python",
        description="Work on Python code",
        body="Python instructions.",
    )

    result = CliRunner().invoke(cli_mod.app, ["skill", "list", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "python" in result.output
    assert "project" in result.output
    assert "native" in result.output
    assert ".alysis_skills" in result.output


def test_skill_info_cli_shows_skill_details(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        ".agents/skills",
        "docker",
        name="docker",
        description="Inspect docker services",
        body="Use docker-compose commands carefully.",
    )

    result = CliRunner().invoke(
        cli_mod.app,
        ["skill", "info", "docker", "--path", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "name: docker" in result.output
    assert "description: Inspect docker services" in result.output
    assert "Use docker-compose commands carefully." in result.output


def test_skill_list_and_info_mark_bundled_skills(tmp_path: Path) -> None:
    runner = CliRunner()

    listed = runner.invoke(cli_mod.app, ["skill", "list", "--path", str(tmp_path)])
    info = runner.invoke(
        cli_mod.app,
        ["skill", "info", "code-review", "--path", str(tmp_path)],
    )

    assert listed.exit_code == 0
    assert "code-review (bundled)" in listed.output
    assert info.exit_code == 0
    assert "source_scope: bundled" in info.output
    # Rich wraps long checkout paths; this checks the source, not terminal layout.
    assert "skills/bundled/code-review" in "".join(info.output.replace("\\", "/").split())


def test_chat_skill_command_lists_discovered_skills_and_info(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        ".alysis_skills",
        "python",
        name="python",
        description="Work on Python code",
        body="Python instructions.",
    )
    discovered = discover_skills(focus_path=tmp_path, workspace_root=tmp_path)

    session = type("Session", (), {})()
    session.cfg = AppConfig(model="test-model", skills_enabled=True)
    session.skill_registry = discovered.skills
    session.skills_ordered = discovered.ordered
    session.skill_discovery_issues = discovered.issues
    session.root = tmp_path
    session.mode = "review"

    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False)
    forge_state = cli_mod._ForgeChatState()

    result = cli_mod._handle_chat_command(
        input_text="/skill",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=console,
        forge_state=forge_state,
    )

    assert result == "handled"
    assert f"Skills ({len(discovered.ordered)})" in stream.getvalue()
    assert "python" in stream.getvalue()

    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False)
    result = cli_mod._handle_chat_command(
        input_text="/skills",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=console,
        forge_state=forge_state,
    )

    assert result == "handled"
    assert "Unknown command: /skills." in stream.getvalue()
    assert "Did you mean /skill? Try /help." in stream.getvalue()

    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False)
    result = cli_mod._handle_chat_command(
        input_text="/skill python",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=console,
        forge_state=forge_state,
    )

    assert result == "handled"
    assert "name: python" in stream.getvalue()
    assert "Python instructions." in stream.getvalue()


def test_chat_skill_list_marks_bundled_skills(tmp_path: Path) -> None:
    discovered = discover_skills(
        focus_path=tmp_path,
        workspace_root=tmp_path,
        user_config_dir=tmp_path / "empty-user-config",
        home_dir=tmp_path / "empty-home",
    )
    session = type("Session", (), {})()
    session.cfg = AppConfig(model="test-model", skills_enabled=True)
    session.skill_registry = discovered.skills
    session.skills_ordered = discovered.ordered
    session.skill_discovery_issues = discovered.issues
    session.root = tmp_path
    session.mode = "review"
    stream = io.StringIO()

    result = cli_mod._handle_chat_command(
        input_text="/skill",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=stream, force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "handled"
    assert "code-review (bundled)" in stream.getvalue()


def test_chat_skill_command_returns_one_turn_execution_request(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        ".alysis_skills",
        "python",
        name="python",
        description="Work on Python code",
        body='Python instructions for "$ARGUMENTS" with $1 and $2.',
    )
    discovered = discover_skills(focus_path=tmp_path, workspace_root=tmp_path)

    session = type("Session", (), {})()
    session.cfg = AppConfig(model="test-model", skills_enabled=True)
    session.skill_registry = discovered.skills
    session.skills_ordered = discovered.ordered
    session.skill_discovery_issues = discovered.issues
    session.root = tmp_path
    session.mode = "review"

    console = Console(file=io.StringIO(), force_terminal=False)
    result = cli_mod._handle_chat_command(
        input_text="/skill python add retries to the parser",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=console,
        forge_state=cli_mod._ForgeChatState(),
    )

    assert isinstance(result, cli_mod._ChatExecutionRequest)
    assert result.instruction == "add retries to the parser"
    assert result.routing_mode_override is None
    assert result.ephemeral_user_messages
    assert "<explicit_skill_context>" in result.ephemeral_user_messages[0]
    assert (
        "turn_requirement: Apply this selected skill before taking other actions on the next user task."
        in result.ephemeral_user_messages[0]
    )
    assert (
        "task_binding: Treat this wrapper and the next user message as one bound instruction set."
        in result.ephemeral_user_messages[0]
    )
    assert "name: python" in result.ephemeral_user_messages[0]
    assert '- $ARGUMENTS = "add retries to the parser"' in result.ephemeral_user_messages[0]
    assert '- $1 = "add"' in result.ephemeral_user_messages[0]
    assert '- $2 = "retries"' in result.ephemeral_user_messages[0]
    assert (
        'Python instructions for "add retries to the parser" with add and retries.'
        in result.ephemeral_user_messages[0]
    )


def test_idle_dollar_skill_task_returns_one_turn_execution_request(tmp_path: Path) -> None:
    discovered = discover_skills(focus_path=tmp_path, workspace_root=tmp_path)
    session = type("Session", (), {})()
    session.cfg = AppConfig(model="test-model", skills_enabled=True)
    session.skill_registry = discovered.skills
    session.skills_ordered = discovered.ordered
    session.skill_discovery_issues = discovered.issues
    session.root = tmp_path
    session.mode = "review"
    literal = "$code-review inspect the staged diff"

    before = cli_mod._handle_chat_command(
        input_text=literal,
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=io.StringIO(), force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )
    handler = getattr(chat_commands, "_handle_idle_skill_invocation", None)

    assert before == "send"
    assert callable(handler)
    result = handler(
        input_text=literal,
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=io.StringIO(), force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert isinstance(result, cli_mod._ChatExecutionRequest)
    assert result.instruction == "inspect the staged diff"
    assert result.ephemeral_user_messages
    assert "name: code-review" in result.ephemeral_user_messages[0]


def test_tui_command_runner_dispatches_dollar_skill_without_keyword_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alysis_code.cli_impl import tui as tui_pkg

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(tui_pkg, "is_tui_enabled", lambda: True)
    for name in (
        "_set_chat_usage_hud_enabled",
        "_apply_startup_persona",
        "_sync_tui_session_state",
        "_refresh_chat_hud_context_cache",
    ):
        monkeypatch.setattr(cli_mod, name, lambda *_args, **_kwargs: None, raising=False)

    discovered = discover_skills(focus_path=tmp_path, workspace_root=tmp_path)

    def _create_session(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            cfg=kwargs["cfg"].model_copy(deep=True),
            root=kwargs["root"],
            mode=kwargs["mode"],
            surface=kwargs["surface"],
            store=SimpleNamespace(append=lambda *_args, **_kwargs: None),
            skill_registry=discovered.skills,
            skills_ordered=discovered.ordered,
            skill_discovery_issues=discovered.issues,
            close=lambda: None,
        )

    observed: list[tuple[str, str, str | None, dict[str, Any] | None]] = []

    def _run_tui(_state: Any, **kwargs: Any) -> tuple[str, list[Any]]:
        session = kwargs["session_builder"](SimpleNamespace())
        observed.append(
            kwargs["command_runner"](
                session,
                "$code-review inspect the staged diff",
                100,
            )
        )
        return "/exit", []

    monkeypatch.setattr(cli_mod, "create_session", _create_session)
    monkeypatch.setattr(tui_pkg, "run_tui", _run_tui)

    result = CliRunner().invoke(
        cli_mod.app,
        [
            "chat",
            "--path",
            os.fspath(tmp_path),
            "--model",
            "test-model",
            "--base-url",
            "https://example.test/v1",
            "--api-key",
            "test-key",
            "--no-log",
        ],
    )

    assert result.exit_code == 0, result.output
    [(action, output, instruction, run_kwargs)] = observed
    assert action == "run", output
    assert instruction == "inspect the staged diff"
    assert run_kwargs is not None
    assert "<explicit_skill_context>" in run_kwargs["ephemeral_user_messages"][0]


def test_idle_bare_dollar_skill_shows_info(tmp_path: Path) -> None:
    discovered = discover_skills(focus_path=tmp_path, workspace_root=tmp_path)
    session = type("Session", (), {})()
    session.cfg = AppConfig(model="test-model", skills_enabled=True)
    session.skill_registry = discovered.skills
    session.skills_ordered = discovered.ordered
    session.skill_discovery_issues = discovered.issues
    session.root = tmp_path
    session.mode = "review"
    stream = io.StringIO()
    handler = getattr(chat_commands, "_handle_idle_skill_invocation", None)

    assert callable(handler)
    result = handler(
        input_text="$code-review",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=stream, force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "handled"
    assert "name: code-review" in stream.getvalue()
    assert "source_scope: bundled" in stream.getvalue()


@pytest.mark.parametrize(
    "literal",
    (
        "$code-revie",
        "$100 is my budget",
        "$PATH is empty; why?",
    ),
)
def test_idle_unknown_dollar_token_remains_user_message(
    tmp_path: Path,
    literal: str,
) -> None:
    discovered = discover_skills(focus_path=tmp_path, workspace_root=tmp_path)
    session = type("Session", (), {})()
    session.cfg = AppConfig(model="test-model", skills_enabled=True)
    session.skill_registry = discovered.skills
    session.skills_ordered = discovered.ordered
    session.skill_discovery_issues = discovered.issues
    session.root = tmp_path
    session.mode = "review"
    stream = io.StringIO()
    handler = getattr(chat_commands, "_handle_idle_skill_invocation", None)

    assert callable(handler)
    result = handler(
        input_text=literal,
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=stream, force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result is None
    assert stream.getvalue() == ""
    assert (
        cli_mod._handle_chat_command(
            input_text=literal,
            root=tmp_path,
            session=session,
            pending_images=[],
            console=Console(file=io.StringIO(), force_terminal=False),
            forge_state=cli_mod._ForgeChatState(),
        )
        == "send"
    )


def test_idle_dollar_token_does_not_resolve_hidden_skill_alias(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        ".alysis_skills",
        "PATH",
        name="safe-skill",
        description="Perform a safe workflow.",
        body="SAFE SKILL BODY",
    )
    discovered = discover_skills(focus_path=tmp_path, workspace_root=tmp_path)
    session = type("Session", (), {})()
    session.cfg = AppConfig(model="test-model", skills_enabled=True)
    session.skill_registry = discovered.skills
    session.skills_ordered = discovered.ordered
    session.skill_discovery_issues = discovered.issues
    session.root = tmp_path
    session.mode = "review"
    stream = io.StringIO()

    result = chat_commands._handle_idle_skill_invocation(
        input_text="$PATH is empty; why?",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=stream, force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result is None
    assert stream.getvalue() == ""


def test_chat_help_documents_dollar_skill_syntax(tmp_path: Path) -> None:
    session = type("Session", (), {})()
    stream = io.StringIO()

    result = cli_mod._handle_chat_command(
        input_text="/help",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=stream, force_terminal=False, width=120),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "handled"
    assert "$<name>" in stream.getvalue()
    assert "show skill info or attach it for one turn" in stream.getvalue()


def test_chat_skill_command_rejects_when_skills_are_disabled(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        ".alysis_skills",
        "python",
        name="python",
        description="Work on Python code",
        body="Python instructions.",
    )
    discovered = discover_skills(focus_path=tmp_path, workspace_root=tmp_path)

    session = type("Session", (), {})()
    session.cfg = AppConfig(model="test-model", skills_enabled=False)
    session.skill_registry = discovered.skills
    session.skills_ordered = discovered.ordered
    session.skill_discovery_issues = discovered.issues
    session.root = tmp_path
    session.mode = "review"

    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False)
    result = cli_mod._handle_chat_command(
        input_text="/skill python add retries to the parser",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=console,
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "handled"
    assert "Skills are disabled for this session config." in stream.getvalue()


def test_cli_skill_list_and_info_skip_invalid_utf8_skill_without_crashing(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        ".alysis_skills",
        "python",
        name="python",
        description="Work on Python code",
        body="Python instructions.",
    )
    _write_invalid_utf8_skill(tmp_path, ".alysis_skills", "broken")

    list_result = CliRunner().invoke(cli_mod.app, ["skill", "list", "--path", str(tmp_path)])
    info_result = CliRunner().invoke(
        cli_mod.app,
        ["skill", "info", "python", "--path", str(tmp_path)],
    )

    assert list_result.exit_code == 0
    assert "python" in list_result.output
    assert "Skipped skill:" in list_result.output
    assert "UTF-8" in list_result.output

    assert info_result.exit_code == 0
    assert "name: python" in info_result.output
