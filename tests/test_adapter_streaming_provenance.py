"""Every Terminal-Bench launcher streams, and says so on the command line.

``--no-stream`` came in with the adapters without a recorded reason; the MiMo
token-plan endpoint closes a non-streaming request at 900 s.
Each launcher now passes ``--stream`` and ``--strict-provenance``, and forwards
the host's thinking setting so a strict run can start.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.cli import app as alysis_app
from alysis_code.config import load_config, save_config, set_config_value
from scripts.benchmarks.terminal_bench import adapter_provenance as provenance_mod
from scripts.benchmarks.terminal_bench import box_harbor_agent as box_adapter_mod
from scripts.benchmarks.terminal_bench.alysis_agent import AlysisSimpleAgent
from scripts.benchmarks.terminal_bench.box_harbor_agent import AlysisAgent as BoxAlysisAgent
from scripts.benchmarks.terminal_bench.harbor_agent import AlysisHarborAgent


def _run_parts(command: str) -> list[str]:
    """The ``alysis run`` arguments up to the ``--`` that precedes the instruction."""
    parts = shlex.split(command.split("alysis run", 1)[1].split("</dev/null", 1)[0], posix=True)
    return parts[: parts.index("--")]


def _assert_streaming_and_strict(parts: list[str]) -> None:
    assert "--no-stream" not in parts
    assert parts.count("--stream") == 1
    assert parts.count("--strict-provenance") == 1


def _harbor_agent() -> AlysisHarborAgent:
    return AlysisHarborAgent(
        logs_dir=".",
        api_key="SECRET-KEY",
        model_name="mimo-v2.5",
        base_url="https://example.invalid/v1",
        command_timeout_sec=7200,
        shutdown_reserve_sec=120,
    )


def _simple_agent() -> AlysisSimpleAgent:
    return AlysisSimpleAgent(
        api_key="SECRET-KEY",
        model_name="mimo-v2.5",
        base_url="https://example.invalid/v1",
        managed_host_agent_timeout_sec=100,
        managed_host_shutdown_reserve_sec=10,
    )


def _box_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra_env: dict[str, str] | None = None
) -> BoxAlysisAgent:
    monkeypatch.setattr(
        box_adapter_mod,
        "evaluate_adapter_provenance",
        lambda **_kwargs: provenance_mod.ProvenanceDecision(failures=(), override_reason=""),
    )
    logs = tmp_path / "logs" / "task__abc1234"
    logs.mkdir(parents=True, exist_ok=True)
    wheel = tmp_path / "alysis-0.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    return BoxAlysisAgent(
        logs_dir=logs,
        extra_env={
            "ALYSIS_WHEEL": str(wheel),
            "ALYSIS_MODEL": "mimo-v2.5",
            "ALYSIS_BASE_URL": "https://example.invalid/v1",
            "ALYSIS_API_KEY": "SECRET-KEY",
            "ALYSIS_MANAGED_HOST_AGENT_TIMEOUT_SEC": "3000",
            **(extra_env or {}),
        },
    )


def _box_run_command(agent: BoxAlysisAgent) -> tuple[str, dict[str, str]]:
    calls: list[tuple[str, dict[str, str]]] = []

    async def fake_exec_as_agent(
        _environment: object, *, command: str, env: dict[str, str], **_kwargs: Any
    ) -> None:
        calls.append((command, dict(env)))

    agent.exec_as_agent = fake_exec_as_agent  # type: ignore[attr-defined,method-assign]
    asyncio.run(agent.run("do the task", object(), SimpleNamespace(metadata={})))  # type: ignore[arg-type]
    return next(call for call in calls if "alysis run" in call[0])


def test_harbor_launcher_streams_with_strict_provenance() -> None:
    _assert_streaming_and_strict(_run_parts(_harbor_agent()._build_run_command("do work")))


def test_simple_launcher_streams_with_strict_provenance() -> None:
    command = _simple_agent()._run_agent_commands("do work")[0].command
    _assert_streaming_and_strict(_run_parts(command))


def test_box_launcher_streams_with_strict_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command, _env = _box_run_command(_box_agent(monkeypatch, tmp_path))
    _assert_streaming_and_strict(_run_parts(command))


@pytest.mark.parametrize("flag", ["--no-stream", "--stream", "--strict-provenance"])
def test_box_extra_args_cannot_override_streaming_or_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flag: str
) -> None:
    agent = _box_agent(monkeypatch, tmp_path, {"ALYSIS_EXTRA_ARGS": f"--max-steps 5 {flag}"})

    with pytest.raises(ValueError, match="cannot override streaming or provenance"):
        _box_run_command(agent)


def test_every_launcher_forwards_the_host_thinking_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ALYSIS_LLM_ENABLE_THINKING", "true")

    assert _harbor_agent()._runtime_env()["ALYSIS_LLM_ENABLE_THINKING"] == "true"
    assert _simple_agent()._env["ALYSIS_LLM_ENABLE_THINKING"] == "true"
    box = _box_agent(monkeypatch, tmp_path, {"ALYSIS_LLM_ENABLE_THINKING": "true"})
    _command, env = _box_run_command(box)
    assert env["ALYSIS_LLM_ENABLE_THINKING"] == "true"


def test_no_thinking_setting_is_invented_when_the_host_sets_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ALYSIS_LLM_ENABLE_THINKING", raising=False)

    assert "ALYSIS_LLM_ENABLE_THINKING" not in _harbor_agent()._runtime_env()
    assert "ALYSIS_LLM_ENABLE_THINKING" not in _simple_agent()._env
    _command, env = _box_run_command(_box_agent(monkeypatch, tmp_path))
    assert "ALYSIS_LLM_ENABLE_THINKING" not in env


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        pytest.param((), True, id="no-flag"),
        pytest.param(("--no-stream",), False, id="no-stream"),
        pytest.param(("--stream",), True, id="stream"),
    ],
)
def test_run_ignores_the_config_stream_value_setup_sh_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flags: tuple[str, ...], expected: bool
) -> None:
    # setup.sh writes `stream false` into the container config. `alysis run`
    # never read it: without a flag it streams, so only the flag decides.
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    save_config(set_config_value(load_config(), "stream", "false"))
    assert load_config().stream is False
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli_mod, "run_agent", lambda **kwargs: calls.append(kwargs) or 0)

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
            "k",
            *flags,
            "hi",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["cfg"].stream is expected
