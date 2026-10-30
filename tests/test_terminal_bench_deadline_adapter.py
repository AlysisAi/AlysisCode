from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import pytest

from scripts.benchmarks.terminal_bench import alysis_agent as adapter_mod
from scripts.benchmarks.terminal_bench.alysis_agent import (
    MANAGED_HOST_DEADLINE_DIAGNOSTIC_FILENAME,
    MANAGED_HOST_SHUTDOWN_RESERVE_ENV,
    VERIFY_CMD_ENV,
    AlysisSimpleAgent,
)


class _SessionSpy:
    def __init__(self) -> None:
        self.copied: list[tuple[Any, dict[str, Any]]] = []
        self.commands: list[Any] = []

    def copy_to_container(self, *args: Any, **kwargs: Any) -> None:
        self.copied.append((args, kwargs))

    def send_command(self, command: Any) -> None:
        self.commands.append(command)


def _agent(**kwargs: Any) -> AlysisSimpleAgent:
    defaults: dict[str, Any] = {
        "api_key": "SECRET-KEY",
        "model_name": "test-model",
        "base_url": "https://example.invalid/v1",
        "managed_host_agent_timeout_sec": 100,
        "managed_host_shutdown_reserve_sec": 10,
    }
    defaults.update(kwargs)
    return AlysisSimpleAgent(**defaults)


def _split_command(agent: AlysisSimpleAgent, instruction: str) -> tuple[list[str], Any]:
    command = agent._run_agent_commands(instruction)[0]
    return shlex.split(command.command, posix=True), command


def test_source_snapshot_retains_build_hook_and_protocol_resource(tmp_path, monkeypatch):
    source = tmp_path / "source"
    files = {
        "pyproject.toml": '[tool.hatch.build.hooks.custom]\npath = "hatch_build.py"\n',
        "hatch_build.py": "# custom build hook\n",
        "src/alysis_code/__init__.py": "",
        "src/alysis_code/forge_host_protocol/alysis-code.schema.json": '{"type":"object"}',
    }
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(adapter_mod, "_REPO_ROOT", source)
    snapshot = tmp_path / "uploaded"
    _agent()._copy_source_snapshot(snapshot)
    for name, content in files.items():
        assert (snapshot / name).read_text(encoding="utf-8") == content


def test_adapter_command_includes_required_deadline_flags_and_separator() -> None:
    instruction = "--starts-with-dash\nquote 'x' and shell $(echo nope) unicode: Δοκιμή"
    parts, command = _split_command(_agent(), instruction)

    assert parts[0].startswith("ALYSIS_MANAGED_HOST_DEADLINE_UNIX_SECONDS=")
    assert parts[1:3] == ["alysis", "run"]
    assert parts.count("--deadline-seconds") == 1
    deadline_index = parts.index("--deadline-seconds")
    assert parts[deadline_index + 1] == "90"
    assert parts.count("--require-deadline") == 1
    separator_index = parts.index("--")
    assert parts[separator_index + 1 :] == [instruction]
    assert parts.index("--require-deadline") < separator_index
    assert command.max_timeout_sec == 101
    assert "SECRET-KEY" not in command.command


def test_adapter_default_has_no_host_verify_command() -> None:
    agent = _agent()
    parts, _command = _split_command(agent, "do work")

    assert "--verify-cmd" not in parts
    assert VERIFY_CMD_ENV not in agent._env


def test_adapter_appends_one_verify_flag_per_explicit_command() -> None:
    agent = _agent(verify_cmd=["pytest -q", "ruff check ."])
    parts, _command = _split_command(agent, "do work")

    verify_indices = [index for index, part in enumerate(parts) if part == "--verify-cmd"]
    assert [parts[index + 1] for index in verify_indices] == ["pytest -q", "ruff check ."]
    assert VERIFY_CMD_ENV not in agent._env


def test_adapter_single_explicit_verify_command_is_exported_for_setup() -> None:
    agent = _agent(verify_cmd="pytest -q")
    parts, _command = _split_command(agent, "do work")

    assert parts[parts.index("--verify-cmd") + 1] == "pytest -q"
    assert agent._env[VERIFY_CMD_ENV] == "pytest -q"


def test_adapter_setup_script_does_not_default_verify_commands_to_true() -> None:
    setup_text = (Path(adapter_mod.__file__).with_name("setup.sh")).read_text(encoding="utf-8")

    assert 'ALYSIS_VERIFY_CMD", "true"' not in setup_text
    assert 'or "true"' not in setup_text
    assert "cfg.verify_commands = [verify_cmd.strip()]" in setup_text
    assert "cfg.verify_commands = []" in setup_text
    assert 'cfg.extra_fields["managed_host_verifier_unavailable"] = True' in setup_text


def test_adapter_rejects_simultaneous_verify_cmd_and_verify_cmds() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        _agent(verify_cmd="pytest -q", verify_cmds=["ruff check ."])


def test_adapter_rejects_empty_explicit_verify_command() -> None:
    with pytest.raises(ValueError, match="verifier command is empty"):
        _agent(verify_cmd="")


def test_adapter_rejects_empty_member_in_explicit_verify_commands() -> None:
    with pytest.raises(ValueError, match="verifier command is empty"):
        _agent(verify_cmd=["pytest -q", " "])


def test_adapter_rejects_unordered_verify_command_set() -> None:
    with pytest.raises(ValueError, match="ordered sequence"):
        _agent(verify_cmd={"pytest -q", "ruff check ."})


def test_adapter_rejects_vacuous_explicit_host_verifier_before_setup(tmp_path: Path) -> None:
    agent = _agent(verify_cmd="true")
    session = _SessionSpy()

    with pytest.raises(ValueError, match="vacuous_verifier"):
        agent.perform_task("do not launch", session, logging_dir=tmp_path)

    assert session.copied == []
    assert session.commands == []
    record = json.loads(
        (tmp_path / MANAGED_HOST_DEADLINE_DIAGNOSTIC_FILENAME).read_text(encoding="utf-8")
    )
    assert record["status"] == "blocked"
    assert record["validation_error"] == "invalid_host_verifier"
    assert record["host_verifier_status"] == "provided"
    assert record["host_verifier_count"] == 1
    assert record["host_verifier_rejection_reasons"] == ["vacuous_verifier"]
    serialized = json.dumps(record)
    assert "do not launch" not in serialized
    assert "SECRET-KEY" not in serialized
    assert "true" not in serialized


def test_adapter_uses_monotonic_elapsed_time_before_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent = _agent(managed_host_agent_timeout_sec=100, managed_host_shutdown_reserve_sec=10)
    agent._managed_host_started_at_monotonic = 50.0
    agent._managed_host_logging_dir = tmp_path
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: 62.5)

    parts, command = _split_command(agent, "do work")

    assert parts[parts.index("--deadline-seconds") + 1] == "77.5"
    assert command.max_timeout_sec == 88.5
    record = json.loads(
        (tmp_path / MANAGED_HOST_DEADLINE_DIAGNOSTIC_FILENAME).read_text(encoding="utf-8")
    )
    assert record["status"] == "ok"
    assert record["final_effective_host_agent_timeout_seconds"] == 100.0
    assert record["elapsed_before_launch_seconds"] == 12.5
    assert record["host_shutdown_reserve_seconds"] == 10.0
    assert record["alysis_invocation_deadline_seconds"] == 77.5
    assert record["terminal_command_timeout_seconds"] == 88.5
    assert record["host_verifier_status"] == "unavailable"
    assert record["host_verifier_count"] == 0


def test_adapter_shutdown_reserve_kwarg_takes_precedence_over_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MANAGED_HOST_SHUTDOWN_RESERVE_ENV, "7")
    parts, _command = _split_command(
        _agent(managed_host_agent_timeout_sec=100, managed_host_shutdown_reserve_sec=12),
        "do work",
    )

    assert parts[parts.index("--deadline-seconds") + 1] == "88"


def test_adapter_uses_shutdown_reserve_environment_when_kwarg_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MANAGED_HOST_SHUTDOWN_RESERVE_ENV, "7.5")
    agent = AlysisSimpleAgent(
        api_key="SECRET-KEY",
        model_name="test-model",
        managed_host_agent_timeout_sec=100,
    )

    parts, _command = _split_command(agent, "do work")

    assert parts[parts.index("--deadline-seconds") + 1] == "92.5"


def test_adapter_fail_closed_when_authoritative_host_timeout_missing(
    tmp_path: Path,
) -> None:
    agent = AlysisSimpleAgent(api_key="SECRET-KEY", model_name="test-model")
    session = _SessionSpy()

    with pytest.raises(ValueError, match="managed-host deadline"):
        agent.perform_task("do not launch", session, logging_dir=tmp_path)

    assert session.copied == []
    assert session.commands == []
    record = json.loads(
        (tmp_path / MANAGED_HOST_DEADLINE_DIAGNOSTIC_FILENAME).read_text(encoding="utf-8")
    )
    assert record["status"] == "blocked"
    assert record["timeout_source"] == "absent"
    assert record["validation_error"] == "final_effective_host_agent_timeout_seconds_missing"
    serialized = json.dumps(record)
    assert "do not launch" not in serialized
    assert "SECRET-KEY" not in serialized


def test_adapter_fail_closed_when_elapsed_time_consumes_launch_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent = _agent(managed_host_agent_timeout_sec=20, managed_host_shutdown_reserve_sec=5)
    agent._managed_host_started_at_monotonic = 10.0
    agent._managed_host_logging_dir = tmp_path
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: 24.5)

    with pytest.raises(ValueError, match="too small to launch"):
        agent._run_agent_commands("do not launch")

    record = json.loads(
        (tmp_path / MANAGED_HOST_DEADLINE_DIAGNOSTIC_FILENAME).read_text(encoding="utf-8")
    )
    assert record["status"] == "blocked"
    assert record["validation_error"] == "remaining_duration_too_small"
    assert record["alysis_invocation_deadline_seconds"] == 0.5


def test_adapter_outer_inner_boundary_keeps_host_reserve_available() -> None:
    parts, command = _split_command(
        _agent(managed_host_agent_timeout_sec=60, managed_host_shutdown_reserve_sec=8),
        "do work",
    )
    deadline_seconds = float(parts[parts.index("--deadline-seconds") + 1])
    simulated_host_remaining = command.max_timeout_sec - 1.0

    assert deadline_seconds == 52
    assert simulated_host_remaining == 60
    assert simulated_host_remaining - deadline_seconds == 8
    assert command.max_timeout_sec > deadline_seconds


def test_adapter_perform_task_launches_exactly_one_deadline_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_base_perform_task(
        self: AlysisSimpleAgent,
        instruction: str,
        session: _SessionSpy,
        logging_dir: Path | None = None,
    ) -> Any:
        _ = logging_dir
        for command in self._run_agent_commands(instruction):
            session.send_command(command)
        return adapter_mod.AgentResult(total_input_tokens=0, total_output_tokens=0)

    monkeypatch.setattr(adapter_mod.AbstractInstalledAgent, "perform_task", fake_base_perform_task)
    agent = _agent(managed_host_agent_timeout_sec=100, managed_host_shutdown_reserve_sec=10)
    session = _SessionSpy()

    result = agent.perform_task("finish this", session, logging_dir=tmp_path)

    assert result.total_input_tokens == 0
    assert len(session.copied) == 1
    assert len(session.commands) == 1
    parts = shlex.split(session.commands[0].command, posix=True)
    assert parts.count("--deadline-seconds") == 1
    assert parts.count("--require-deadline") == 1
