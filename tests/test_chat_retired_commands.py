"""Commands that only Forge offered are ordinary unknown commands in chat."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from alysis_code import agent_loop as agent_loop_mod
from alysis_code import cli as cli_mod
from alysis_code.cli import app as alysis_app
from alysis_code.cli_impl.chat_slash_completer import get_chat_specs
from alysis_code.config import AppConfig
from alysis_code.session_store import read_session_events

# Commands the classic chat accepted only inside Forge. Each is now a plain unknown command.
_RETIRED_COMMANDS = (
    "/forge",
    "/forge status",
    "/plan",
    "/plan implement feature",
    "/plan approve",
    "/back",
    "/assets",
    "/execute plan",
    "/task add a login page",
    "/show",
    "/done",
    "/assistant on",
)


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }


@pytest.mark.parametrize("command", _RETIRED_COMMANDS)
def test_retired_forge_command_is_unknown_and_the_next_message_runs_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"run_turn_calls": 0, "rebuild_modes": []}

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"
        temperature = 1.0

        @staticmethod
        def chat(**_kwargs: Any) -> Any:
            raise AssertionError(f"{command} must not call the model")

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="test-model", default_mode="review")

        def run_turn(self, instruction: str, **_kwargs: Any) -> int:
            captured["run_turn_calls"] += 1
            captured["instruction"] = instruction
            return 0

        @staticmethod
        def close() -> None:
            return None

    session = _DummySession()
    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: session)
    monkeypatch.setattr(
        cli_mod,
        "_rebuild_session_tools_for_mode",
        lambda *, session, mode: captured["rebuild_modes"].append(mode),
    )

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input=f"{command}\nhello\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert f"Unknown command: {command.split()[0]}" in result.output
    assert captured["rebuild_modes"] == []
    assert session.mode == "review"
    assert captured["run_turn_calls"] == 1
    assert captured["instruction"] == "hello"


def test_chat_lists_and_help_never_offer_retired_forge_commands() -> None:
    retired = {command.split()[0] for command in _RETIRED_COMMANDS}
    completer_names = {f"/{spec.name}" for spec in get_chat_specs()}
    help_commands = {
        cmd.split()[0] for _section, rows in cli_mod._chat_command_sections() for cmd, _ in rows
    }
    visible = {cmd.split()[0] for cmd in cli_mod._chat_visible_commands()}

    assert not retired & completer_names
    assert not retired & help_commands
    assert not retired & visible


def test_code_only_turn_ephemeral_system_prompts_do_not_pollute_persisted_history(
    tmp_path: Path,
) -> None:
    internal_prompt = (
        "You are handling one host-injected turn.\n"
        "Do not write files. Do not run shell commands. Planning text only."
    )
    reply = "We were focused on turn understanding."

    class _RepoClient:
        model = "test-model"
        temperature = 0.2

        def chat(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                content=reply,
                usage=None,
                response_model=self.model,
                tool_calls=[],
            )

    session = agent_loop_mod.create_session(
        cfg=AppConfig(model="test-model", routing_mode="auto"),
        root=tmp_path,
        mode="readonly",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
    )
    session.client = _RepoClient()  # type: ignore[assignment]
    session.router_client = None
    try:
        exit_code = session.run_turn(
            "let's work",
            routing_mode_override="code_only",
            ephemeral_system_messages=[internal_prompt],
        )
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    events = list(read_session_events(log_path))
    serialized_events = [json.dumps(event, ensure_ascii=False) for event in events]
    assert all(internal_prompt not in item for item in serialized_events)
    assert any(
        str(event.get("type") or "") == "user_message"
        and str((event.get("payload") or {}).get("content") or "") == "let's work"
        for event in events
    )
    assert any(
        str(event.get("type") or "") == "assistant_message"
        and str((event.get("payload") or {}).get("content") or "") == reply
        for event in events
    )
