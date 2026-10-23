"""Harbor runs record the prompt profile their session resolved.

Comparing runs across builds needs every session log to name the prompt
profile the run used. Both MiMo models are reviewed as ``mimo-expanded``.
These tests take the ``alysis run`` command line each Harbor launcher builds,
pass it through the CLI up to the point where the agent starts, open a session
from the resulting config in a container-like config directory, and read the
profile back from the session log.
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
from alysis_code.agent_loop import create_session
from alysis_code.cli import app as alysis_app
from alysis_code.config import AppConfig, load_config, save_config, set_config_value
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.session_store import read_session_events
from alysis_code.surface.noop_surface import NoopSurface
from scripts.benchmarks.terminal_bench import adapter_provenance as provenance_mod
from scripts.benchmarks.terminal_bench import box_harbor_agent as box_adapter_mod
from scripts.benchmarks.terminal_bench.box_harbor_agent import AlysisAgent as BoxAlysisAgent
from scripts.benchmarks.terminal_bench.harbor_agent import AlysisHarborAgent

_MIMO_TOKEN_PLAN = "https://token-plan-ams.xiaomimimo.com/v1"
_MIMO_MODELS = ("mimo-v2.5", "mimo-v2.6-flash")


def _container_config(model: str) -> None:
    """What setup.sh leaves in the container config before any run."""
    cfg = load_config()
    add_profile(
        cfg,
        ProfileSpec(
            name="tbench",
            base_url=_MIMO_TOKEN_PLAN,
            api_key_env="ALYSIS_API_KEY",
            default_model=model,
            web_search_adapter="auto",
            web_search_model="",
            notes="Terminal-Bench Xiaomi MiMo through the Alysis Code proxy.",
        ),
    )
    set_active_profile(cfg, "tbench")
    cfg.web_search_mode = "off"
    cfg.default_mode = "fullaccess"
    cfg.stream = False
    save_config(cfg)


def _run_args(command: str, workspace: Path) -> list[str]:
    """The ``alysis run`` arguments, pointed at ``workspace``."""
    parts = shlex.split(command.split("alysis run", 1)[1].split("</dev/null", 1)[0], posix=True)
    args = parts[: parts.index("--")]
    args[args.index("--path") + 1] = os.fspath(workspace)
    return ["run", *args, "--", "do the task"]


def _harbor_command(model: str) -> str:
    agent = AlysisHarborAgent(
        logs_dir=".",
        api_key="SECRET-KEY",
        model_name=model,
        base_url=_MIMO_TOKEN_PLAN,
        command_timeout_sec=7200,
        shutdown_reserve_sec=120,
    )
    return agent._build_run_command("do the task")


def _box_commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, model: str) -> list[str]:
    monkeypatch.setattr(
        box_adapter_mod,
        "evaluate_adapter_provenance",
        lambda **_kwargs: provenance_mod.ProvenanceDecision(failures=(), override_reason=""),
    )
    logs = tmp_path / "logs" / "task__abc1234"
    logs.mkdir(parents=True, exist_ok=True)
    wheel = tmp_path / "alysis-0.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    agent = BoxAlysisAgent(
        logs_dir=logs,
        extra_env={
            "ALYSIS_WHEEL": os.fspath(wheel),
            "ALYSIS_MODEL": model,
            "ALYSIS_BASE_URL": _MIMO_TOKEN_PLAN,
            "ALYSIS_API_KEY": "SECRET-KEY",
            "ALYSIS_MANAGED_HOST_AGENT_TIMEOUT_SEC": "3000",
            "ALYSIS_LLM_ENABLE_THINKING": "true",
        },
    )
    commands: list[str] = []

    async def fake_exec_as_agent(_environment: object, *, command: str, **_kwargs: Any) -> None:
        commands.append(command)

    agent.exec_as_agent = fake_exec_as_agent  # type: ignore[attr-defined,method-assign]
    asyncio.run(agent.run("do the task", object(), SimpleNamespace(metadata={})))  # type: ignore[arg-type]
    return commands


def _apply_config_set_commands(commands: list[str]) -> None:
    """Apply the launcher's ``alysis config set`` steps to the container config."""
    cfg = load_config()
    for command in commands:
        for step in command.split("&&"):
            parts = shlex.split(step)
            if parts[:3] == ["alysis", "config", "set"] and len(parts) == 5:
                cfg = set_config_value(cfg, parts[3], parts[4])
    save_config(cfg)


def _cli_config(args: list[str], monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli_mod, "run_agent", lambda **kwargs: calls.append(kwargs) or 0)
    result = CliRunner().invoke(alysis_app, args)
    assert result.exit_code == 0, result.output
    cfg = calls[0]["cfg"]
    assert isinstance(cfg, AppConfig)
    return cfg


def _recorded_profiles(cfg: AppConfig, tmp_path: Path) -> tuple[Any, Any, Any]:
    workspace = tmp_path / "session-workspace"
    workspace.mkdir(exist_ok=True)
    session = create_session(
        cfg=cfg,
        root=workspace,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="test-key",
        surface=NoopSurface(),
        session_log_dir_override=tmp_path / "sessions",
    )
    try:
        events = list(read_session_events(session.store.path))
        live_profile = session.prompt_guidance_profile
    finally:
        session.close()
    snapshot = next(event for event in events if event.get("type") == "config_snapshot")
    start = next(event for event in events if event.get("type") == "session_start")
    return (
        snapshot["payload"]["effective"]["prompt_guidance_profile"],
        start["payload"]["prompt_guidance_profile"],
        live_profile,
    )


@pytest.fixture
def container_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_API_KEY", "test-key")
    monkeypatch.setenv("ALYSIS_LLM_ENABLE_THINKING", "true")
    monkeypatch.delenv("ALYSIS_MODEL", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.mark.parametrize("model", _MIMO_MODELS)
def test_harbor_agent_session_records_mimo_expanded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, container_env: Path, model: str
) -> None:
    _container_config(model)
    cfg = _cli_config(_run_args(_harbor_command(model), container_env), monkeypatch)

    assert cfg.model == model
    assert _recorded_profiles(cfg, tmp_path) == ("mimo-expanded",) * 3


@pytest.mark.parametrize("model", _MIMO_MODELS)
def test_box_harbor_agent_session_records_mimo_expanded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, container_env: Path, model: str
) -> None:
    _container_config(model)
    commands = _box_commands(monkeypatch, tmp_path, model)
    _apply_config_set_commands(commands)
    run_command = next(command for command in commands if "alysis run" in command)
    cfg = _cli_config(_run_args(run_command, container_env), monkeypatch)

    assert cfg.model == model
    assert _recorded_profiles(cfg, tmp_path) == ("mimo-expanded",) * 3
