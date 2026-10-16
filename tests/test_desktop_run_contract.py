from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from alysis_code import agent_loop as agent_loop_mod
from alysis_code.cli import app as alysis_app
from alysis_code.cli_impl.chat.loop import _load_run_seed_messages
from alysis_code.config import AppConfig


def _desktop_env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "config"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
    }


def _write_prior_turns(session_path: Path) -> None:
    session_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user_message",
                        "owner": "local-user@local-host",
                        "payload": {"content": "hello"},
                    }
                ),
                json.dumps(
                    {
                        "type": "final",
                        "owner": "local-user@local-host",
                        "payload": {"content": "Hi!"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_run_help_exposes_desktop_transport_options() -> None:
    result = CliRunner().invoke(alysis_app, ["run", "--help"], terminal_width=160)

    assert result.exit_code == 0
    # Rich may truncate or style help according to the runner's terminal. Check
    # the CLI parser's complete option names independently of that presentation.
    run_command = get_command(alysis_app).commands["run"]
    options = {option for parameter in run_command.params for option in parameter.opts}
    assert {"--instruction-stdin", "--session", "--session-dir"} <= options


def test_run_reads_large_instruction_from_stdin_without_echoing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "alysis_code.cli_impl.commands.root._require_active_subscription_ready",
        lambda **_kwargs: None,
    )

    def fake_run_impl(
        _cli_module: object, instruction: str, path: Path, *args: object, **kwargs: object
    ) -> None:
        captured.update(instruction=instruction, path=path, args=args, kwargs=kwargs)

    monkeypatch.setattr("alysis_code.cli_impl.chat.run_impl", fake_run_impl)
    secret = "stdin-only-secret-never-echoed"
    instruction = "[Alysis Desktop context - not typed by the user]\n" + secret + ("x" * 300_000)
    result = CliRunner().invoke(
        alysis_app,
        ["run", "--instruction-stdin", "--path", os.fspath(tmp_path)],
        input=instruction,
        env=_desktop_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert captured["instruction"] == instruction
    assert captured["path"] == tmp_path
    assert secret not in result.output


def test_named_run_reconstructs_prior_turns(tmp_path: Path) -> None:
    session_id = "forge_projectless_chat"
    _write_prior_turns(tmp_path / f"{session_id}.jsonl")

    messages = _load_run_seed_messages(session_id=session_id, session_dir=tmp_path)

    assert messages is not None
    assert messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi!"},
    ]


def test_named_run_rejects_unsafe_session_id_before_agent_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alysis_code import cli as cli_mod

    reached_agent = False

    def fake_run_agent(**_kwargs: object) -> int:
        nonlocal reached_agent
        reached_agent = True
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    result = CliRunner().invoke(
        alysis_app,
        [
            "run",
            "--path",
            os.fspath(tmp_path),
            "--allow-broad-workspace",
            "--model",
            "test-model",
            "--api-key",
            "key",
            "--session",
            "../foreign",
            "continue",
        ],
        env=_desktop_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "--session must be a safe session identifier" in result.output
    assert reached_agent is False


def test_instruction_stdin_conflicts_fail_before_runtime_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached_runtime = False

    def fail_if_called(**_kwargs: object) -> None:
        nonlocal reached_runtime
        reached_runtime = True

    monkeypatch.setattr(
        "alysis_code.cli_impl.commands.root._require_active_subscription_ready",
        fail_if_called,
    )
    result = CliRunner().invoke(
        alysis_app,
        ["run", "positional", "--instruction-stdin"],
        input="stdin instruction",
        env=_desktop_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "Do not combine INSTRUCTION with --instruction-stdin." in result.output
    assert reached_runtime is False


def test_instruction_and_api_key_cannot_share_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached_runtime = False

    def fail_if_called(**_kwargs: object) -> None:
        nonlocal reached_runtime
        reached_runtime = True

    monkeypatch.setattr(
        "alysis_code.cli_impl.commands.root._require_active_subscription_ready",
        fail_if_called,
    )
    result = CliRunner().invoke(
        alysis_app,
        ["run", "--instruction-stdin", "--api-key-stdin"],
        input="must-not-be-reinterpreted-as-a-key",
        env=_desktop_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "Do not combine --instruction-stdin with --api-key-stdin." in result.output
    assert "must-not-be-reinterpreted-as-a-key" not in result.output
    assert reached_runtime is False


def test_persona_run_preserves_desktop_session_transport_and_avoids_duplicate_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alysis_code import cli as cli_mod

    sessions_dir = tmp_path / "desktop-sessions"
    sessions_dir.mkdir()
    session_id = "desktop_persona_chat"
    _write_prior_turns(sessions_dir / f"{session_id}.jsonl")
    captured: dict[str, Any] = {}

    def fake_run_agent(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    instruction = (
        "[Alysis Desktop context - not typed by the user]\n"
        "The App already supplied the authoritative conversation context."
    )
    result = CliRunner().invoke(
        alysis_app,
        [
            "run",
            "--instruction-stdin",
            "--path",
            os.fspath(tmp_path),
            "--allow-broad-workspace",
            "--model",
            "test-model",
            "--api-key",
            "key",
            "--persona",
            "ask",
            "--session",
            session_id,
            "--session-dir",
            os.fspath(sessions_dir),
        ],
        input=instruction,
        env=_desktop_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert captured["instruction"] == instruction
    assert captured["session_id_override"] == session_id
    assert captured["session_log_dir_override"] == sessions_dir
    assert captured["seed_messages"] is None
    assert captured["mode"] == "readonly"
    assert str(captured["runtime_kind"]) == "interactive_chat"
    assert captured["one_shot_execution"] is False


def test_non_desktop_named_run_replays_history_through_persona_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alysis_code import cli as cli_mod

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    session_id = "direct_persona_chat"
    _write_prior_turns(sessions_dir / f"{session_id}.jsonl")
    captured: dict[str, Any] = {}

    def fake_run_agent(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    result = CliRunner().invoke(
        alysis_app,
        [
            "run",
            "--path",
            os.fspath(tmp_path),
            "--allow-broad-workspace",
            "--model",
            "test-model",
            "--api-key",
            "key",
            "--persona",
            "ask",
            "--session",
            session_id,
            "--session-dir",
            os.fspath(sessions_dir),
            "continue",
        ],
        env=_desktop_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert captured["seed_messages"] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi!"},
    ]
    assert str(captured["runtime_kind"]) == "one_shot"
    assert captured["one_shot_execution"] is True


def test_run_agent_inserts_seed_messages_before_the_new_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class FakeSession:
        def __init__(self) -> None:
            self.messages: list[dict[str, Any]] = [{"role": "system", "content": "system"}]

        def run_turn(self, instruction: str, **kwargs: object) -> int:
            observed["instruction"] = instruction
            observed["messages"] = list(self.messages)
            observed["turn_kwargs"] = kwargs
            return 7

        def close(self) -> None:
            observed["closed"] = True

    fake_session = FakeSession()
    monkeypatch.setattr(agent_loop_mod, "create_session", lambda **_kwargs: fake_session)
    seed_messages = [
        {"role": "user", "content": "prior question"},
        {"role": "assistant", "content": "prior answer"},
    ]

    code = agent_loop_mod.run_agent(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        instruction="current question",
        mode="review",
        yes=False,
        max_steps=1,
        no_log=True,
        seed_messages=seed_messages,
    )

    assert code == 7
    assert observed["messages"] == [
        {"role": "system", "content": "system"},
        *seed_messages,
    ]
    assert observed["instruction"] == "current question"
    assert observed["closed"] is True
