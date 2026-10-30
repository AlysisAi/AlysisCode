from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alysis_code.run_outcome import INFRASTRUCTURE_FAILURE_EXIT_CODE, RunOutcome
from scripts.benchmarks.terminal_bench import adapter_provenance as provenance_mod
from scripts.benchmarks.terminal_bench import box_harbor_agent as box_adapter_mod
from scripts.benchmarks.terminal_bench import harbor_agent as harbor_adapter_mod
from scripts.benchmarks.terminal_bench.box_harbor_agent import AlysisAgent as BoxAlysisAgent
from scripts.benchmarks.terminal_bench.harbor_agent import AlysisHarborAgent


def _agent(**kwargs: Any) -> AlysisHarborAgent:
    defaults: dict[str, Any] = {
        "logs_dir": ".",
        "api_key": "SECRET-KEY",
        "model_name": "qwen-test",
        "base_url": "https://example.invalid/v1",
        "command_timeout_sec": 7200,
        "shutdown_reserve_sec": 120,
    }
    defaults.update(kwargs)
    return AlysisHarborAgent(**defaults)


def test_harbor_adapter_builds_command_with_1000_steps() -> None:
    instruction = "--starts-with-dash and quotes 'x'"
    command = _agent()._build_run_command(instruction)
    parts = shlex.split(command, posix=True)

    assert parts[:2] == ["alysis", "run"]
    assert parts[parts.index("--max-steps") + 1] == "1000"
    assert parts[parts.index("--deadline-seconds") + 1] == "7080"
    assert "--require-deadline" in parts
    assert "--subagents" in parts
    assert parts[parts.index("--api-key-env") + 1] == "ALYSIS_API_KEY"
    assert parts[parts.index("--") + 1 :] == [instruction]
    assert "SECRET-KEY" not in command


def test_harbor_adapter_can_disable_subagents() -> None:
    command = _agent(subagents=False)._build_run_command("do work")
    parts = shlex.split(command, posix=True)

    assert "--subagents" not in parts
    assert "--no-subagents" in parts


def test_harbor_install_executes_setup_script_in_uploaded_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    (repo / "hatch_build.py").write_text("# fixture build hook\n", encoding="utf-8")
    schema = Path("src/alysis_code/forge_host_protocol/alysis-code.schema.json")
    (repo / schema).parent.mkdir(parents=True)
    (repo / schema).write_text('{"type":"object"}', encoding="utf-8")
    monkeypatch.setattr(harbor_adapter_mod, "_REPO_ROOT", repo)
    uploaded_files: dict[str, bytes] = {}
    executed: list[str] = []

    class Environment:
        async def upload_dir(self, source: Path, destination: str) -> None:
            for path in source.rglob("*"):
                if path.is_file():
                    uploaded_files[f"{destination}/{path.relative_to(source).as_posix()}"] = (
                        path.read_bytes()
                    )

    async def exec_as_root(_environment: object, *, command: str, **_kwargs: Any) -> None:
        parts = shlex.split(command)
        setup_path = parts[-1]
        assert parts == ["chmod", "+x", setup_path, "&&", setup_path]
        assert uploaded_files[setup_path].startswith(b"#!/bin/sh")
        executed.append(setup_path)

    agent = _agent()
    agent.exec_as_root = exec_as_root  # type: ignore[attr-defined,method-assign]
    asyncio.run(agent.install(Environment()))

    assert executed == ["/installed-agent/alysis-source/scripts/benchmarks/terminal_bench/setup.sh"]
    assert "/installed-agent/alysis-source/src/module.py" in uploaded_files
    assert "/installed-agent/alysis-source/pyproject.toml" in uploaded_files
    assert "/installed-agent/alysis-source/hatch_build.py" in uploaded_files
    assert f"/installed-agent/alysis-source/{schema.as_posix()}" in uploaded_files


def test_harbor_runtime_env_uses_dashscope_key_without_putting_it_in_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "DASH-SECRET")
    agent = _agent(api_key=None)

    env = agent._runtime_env()
    command = agent._build_run_command("do work")

    assert env["ALYSIS_API_KEY"] == "DASH-SECRET"
    assert env["ALYSIS_BASE_URL"] == "https://example.invalid/v1"
    assert "DASH-SECRET" not in command


def test_harbor_runtime_env_accepts_openrouter_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "OR-SECRET")
    agent = _agent(api_key=None, base_url="https://openrouter.ai/api/v1")

    env = agent._runtime_env()
    command = agent._build_run_command("do work")

    assert env["ALYSIS_API_KEY"] == "OR-SECRET"
    assert env["ALYSIS_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert "OR-SECRET" not in command


def test_harbor_runtime_env_fails_closed_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    agent = _agent(api_key=None)

    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        agent._runtime_env()


def test_box_adapter_install_uses_versioned_setup_script_and_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "alysis-0.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    agent = BoxAlysisAgent(
        extra_env={
            "ALYSIS_WHEEL": str(wheel),
            "ALYSIS_MODEL": "mimo-v2.5-pro",
            "ALYSIS_BASE_URL": "https://example.invalid/v1",
            "ALYSIS_API_KEY": "SECRET-KEY",
            "ALYSIS_MANAGED_HOST_AGENT_TIMEOUT_SEC": "3000",
        }
    )

    install_env = agent._install_env()
    install_command = agent._install_command()

    assert agent._host_setup_script_path().endswith("scripts/benchmarks/terminal_bench/setup.sh")
    assert install_env["ALYSIS_WHEEL"] == "/tmp/alysis-agent/" + wheel.name
    assert install_env["ALYSIS_MODEL"] == "mimo-v2.5-pro"
    assert install_env["ALYSIS_BASE_URL"] == "https://example.invalid/v1"
    assert install_env["ALYSIS_SETUP_LOG_DIR"] == "/logs/agent/setup"
    assert install_env["ALYSIS_SETUP_ARTIFACT_DIR"] == "/logs/artifacts/setup"
    assert "ALYSIS_API_KEY" not in install_env
    assert "setup.sh" in install_command
    assert "SECRET-KEY" not in install_command


def test_box_adapter_summary_marks_infrastructure_exit_separately(tmp_path: Path) -> None:
    wheel = tmp_path / "alysis-0.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    agent = BoxAlysisAgent(
        extra_env={
            "ALYSIS_WHEEL": str(wheel),
            "ALYSIS_MODEL": "mimo-v2.5-pro",
            "ALYSIS_BASE_URL": "https://example.invalid/v1",
            "ALYSIS_API_KEY": "SECRET-KEY",
            "ALYSIS_MANAGED_HOST_AGENT_TIMEOUT_SEC": "3000",
        }
    )
    calls = 0

    async def fake_exec_as_agent(
        _environment: object,
        *,
        command: str,
        env: dict[str, str],
        **_kwargs: object,
    ) -> None:
        nonlocal calls
        _ = command, env
        calls += 1
        if calls == 3:
            raise RuntimeError(
                f"Command failed (exit {INFRASTRUCTURE_FAILURE_EXIT_CODE}): provider outage"
            )

    agent.exec_as_agent = fake_exec_as_agent  # type: ignore[attr-defined,method-assign]
    context = SimpleNamespace(metadata={})

    with pytest.raises(RuntimeError, match="provider outage"):
        asyncio.run(agent.run("fix the bug", object(), context))

    assert context.metadata["alysis_exit_code"] == INFRASTRUCTURE_FAILURE_EXIT_CODE
    assert context.metadata["alysis_outcome"] == RunOutcome.INFRA_FAIL.value


def test_terminal_bench_setup_script_retries_network_installs_and_uses_venv() -> None:
    setup_text = (Path(box_adapter_mod.__file__).with_name("setup.sh")).read_text(encoding="utf-8")

    assert setup_text.startswith("#!/bin/sh")
    assert "/logs/agent/setup" in setup_text
    assert "/logs/artifacts/setup" in setup_text
    assert "retry apt-get-update apt-get update" in setup_text
    assert "retry apk-add apk add" in setup_text
    assert "retry dnf-install dnf install" in setup_text
    assert "retry uv-installer" in setup_text
    assert "retry pip-install-alysis" in setup_text
    assert "retry uv-pip-install" in setup_text
    assert "uv python install 3.12" in setup_text
    assert "python3 -m venv /opt/alysis-venv" in setup_text
    assert "ln -sf /opt/alysis-venv/bin/alysis /usr/local/bin/alysis" in setup_text
    assert "ALYSIS_WHEEL" in setup_text
    assert "--break-system-packages" not in setup_text


def test_harbor_runner_raises_verifier_timeout_multiplier() -> None:
    runner_text = (Path(box_adapter_mod.__file__).with_name("run_harbor_tbench.sh")).read_text(
        encoding="utf-8"
    )

    assert 'TB_VERIFIER_TIMEOUT_MULTIPLIER="${TB_VERIFIER_TIMEOUT_MULTIPLIER:-10}"' in runner_text


# ---------------------------------------------------------------------------
# Provenance gate placement
#
# The gate belongs in install(), not in __init__. A constructor that refuses
# cannot be built for inspection, tooling, or these tests, and install() is
# both the earliest point the check can mean anything -- it is where the wheel
# enters the trial -- and the only point it needs to happen, since Harbor
# always installs before it runs.
# ---------------------------------------------------------------------------


def _decision(*, failed: bool = False, reason: str = "") -> Any:
    failures = ()
    if failed:
        failures = (
            provenance_mod.ProvenanceFailure(
                provenance_mod.FAILURE_COMMIT_MISMATCH, "wheel and adapter disagree"
            ),
        )
    return provenance_mod.ProvenanceDecision(failures=failures, override_reason=reason)


class _RecordingEnvironment:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []

    async def upload_file(self, source: str, destination: str) -> None:
        self.uploads.append((source, destination))


def _box_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    decision: Any,
    **extra_env: str,
) -> BoxAlysisAgent:
    monkeypatch.setattr(box_adapter_mod, "evaluate_adapter_provenance", lambda **_kwargs: decision)
    wheel = tmp_path / "alysis-0.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    env = {
        "ALYSIS_WHEEL": str(wheel),
        "ALYSIS_MODEL": "mimo-v2.5-pro",
        "ALYSIS_BASE_URL": "https://example.invalid/v1",
        "ALYSIS_API_KEY": "SECRET-KEY",
        "ALYSIS_MANAGED_HOST_AGENT_TIMEOUT_SEC": "3000",
    }
    env.update(extra_env)
    return BoxAlysisAgent(logs_dir=tmp_path / "logs", extra_env=env)


async def _install(agent: BoxAlysisAgent) -> _RecordingEnvironment:
    environment = _RecordingEnvironment()

    async def fake_exec_as_root(*_args: object, **_kwargs: object) -> None:
        return None

    agent.exec_as_root = fake_exec_as_root  # type: ignore[attr-defined,method-assign]
    await agent.install(environment)
    return environment


def test_constructing_the_adapter_never_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The regression this pins: a failing check used to make the class
    # unconstructable, which broke every test and tool that only wanted to
    # look at the adapter.
    agent = _box_agent(monkeypatch, tmp_path, _decision(failed=True))

    assert agent._provenance.failed is True
    assert agent._install_command()


def test_constructing_the_adapter_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _box_agent(monkeypatch, tmp_path, _decision())

    assert not (tmp_path / "logs" / "alysis-provenance.json").exists()


def test_install_refuses_a_mismatched_wheel_before_uploading_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = _box_agent(monkeypatch, tmp_path, _decision(failed=True))
    environment = _RecordingEnvironment()

    async def fake_exec_as_root(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("install must refuse before touching the container")

    agent.exec_as_root = fake_exec_as_root  # type: ignore[attr-defined,method-assign]

    with pytest.raises(provenance_mod.ProvenanceError, match="provenance check failed"):
        asyncio.run(agent.install(environment))

    assert environment.uploads == []


def test_install_records_provenance_even_when_it_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The refusal is the moment someone most wants the record.
    agent = _box_agent(monkeypatch, tmp_path, _decision(failed=True))

    with pytest.raises(provenance_mod.ProvenanceError):
        asyncio.run(agent.install(_RecordingEnvironment()))

    written = json.loads((tmp_path / "logs" / "alysis-provenance.json").read_text())
    assert written["alysis_provenance"]["failed"] is True
    assert written["alysis_provenance"]["failure_codes"] == [provenance_mod.FAILURE_COMMIT_MISMATCH]


def test_install_proceeds_when_provenance_agrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = _box_agent(monkeypatch, tmp_path, _decision())

    environment = asyncio.run(_install(agent))

    assert len(environment.uploads) == 2
    assert (tmp_path / "logs" / "alysis-provenance.json").exists()


def test_install_proceeds_when_the_failure_is_overridden(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reason = "trial-3 rerun: wheel pinned deliberately, see campaign notes"
    agent = _box_agent(monkeypatch, tmp_path, _decision(failed=True, reason=reason))

    environment = asyncio.run(_install(agent))

    assert len(environment.uploads) == 2
    written = json.loads((tmp_path / "logs" / "alysis-provenance.json").read_text())
    # Recorded, not erased: the failure and the reason travel together.
    assert written["alysis_provenance"]["overridden"] is True
    assert written["alysis_provenance"]["override_reason"] == reason
    assert written["alysis_provenance"]["failure_codes"] == [provenance_mod.FAILURE_COMMIT_MISMATCH]


def test_install_proceeds_when_the_guard_is_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = _box_agent(monkeypatch, tmp_path, _decision(failed=True), ALYSIS_PROVENANCE_GUARD="0")

    environment = asyncio.run(_install(agent))

    assert len(environment.uploads) == 2
    written = json.loads((tmp_path / "logs" / "alysis-provenance.json").read_text())
    assert written["alysis_provenance"]["guard_enabled"] is False


def test_the_provenance_artifact_never_lands_in_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # logs_dir defaults to "." when Harbor configures none, and a benchmark
    # adapter must not drop files into whatever directory the operator
    # launched from. This test caught exactly that leak.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        box_adapter_mod, "evaluate_adapter_provenance", lambda **_kwargs: _decision()
    )
    wheel = tmp_path / "alysis-0.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    agent = BoxAlysisAgent(
        extra_env={
            "ALYSIS_WHEEL": str(wheel),
            "ALYSIS_MODEL": "mimo-v2.5-pro",
            "ALYSIS_BASE_URL": "https://example.invalid/v1",
            "ALYSIS_API_KEY": "SECRET-KEY",
            "ALYSIS_MANAGED_HOST_AGENT_TIMEOUT_SEC": "3000",
        }
    )

    asyncio.run(_install(agent))

    assert not (tmp_path / "alysis-provenance.json").exists()


def _box_agent_for(tmp_path: Path, task_dir: str, extra_env: dict[str, str]) -> BoxAlysisAgent:
    logs = tmp_path / "logs" / task_dir
    logs.mkdir(parents=True, exist_ok=True)
    wheel = tmp_path / "alysis-0.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    return BoxAlysisAgent(
        logs_dir=logs,
        extra_env={
            "ALYSIS_WHEEL": str(wheel),
            "ALYSIS_MODEL": "mimo-v2.5-pro",
            "ALYSIS_BASE_URL": "https://example.invalid/v1",
            "ALYSIS_API_KEY": "SECRET-KEY",
            "ALYSIS_MANAGED_HOST_AGENT_TIMEOUT_SEC": "3000",
            **extra_env,
        },
    )


def _run_and_capture_agent_command(agent: BoxAlysisAgent) -> str:
    commands: list[str] = []

    async def fake_exec_as_agent(
        _environment: object, *, command: str, env: dict[str, str], **_kwargs: object
    ) -> None:
        _ = env
        commands.append(command)

    agent.exec_as_agent = fake_exec_as_agent  # type: ignore[attr-defined,method-assign]
    asyncio.run(agent.run("do the task", object(), SimpleNamespace(metadata={})))
    # Third exec is the `alysis run ...` pipeline.
    return commands[2]


def _forwarded_deadline(command: str) -> str | None:
    parts = shlex.split(command.split("alysis run", 1)[1].split("</dev/null", 1)[0], posix=True)
    if "--deadline-seconds" not in parts:
        return None
    return parts[parts.index("--deadline-seconds") + 1]


def test_config_set_passthrough_reaches_the_container_and_the_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The wheel ships bundled skills with auto-selection on by default; a
    # campaign that wants baseline prompt parity must be able to switch them
    # off inside the container, and prove it did.
    monkeypatch.setattr(
        box_adapter_mod, "evaluate_adapter_provenance", lambda **_kwargs: _decision()
    )
    agent = _box_agent_for(
        tmp_path,
        "overfull-hbox__abc1234",
        {"ALYSIS_CONFIG_SET": "skills_enabled=false; skills_auto_invoke=false"},
    )

    cmds = agent._config_set_cmds()
    fields = agent._run_manifest_fields()

    assert "alysis config set skills_enabled false" in cmds
    assert "alysis config set skills_auto_invoke false" in cmds
    assert fields["config_set_forwarded"] == {
        "skills_enabled": "false",
        "skills_auto_invoke": "false",
    }


def test_config_set_passthrough_drops_malformed_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The string is interpolated into a shell command in the container, so a
    # key that is not a plain config identifier must never become a command.
    monkeypatch.setattr(
        box_adapter_mod, "evaluate_adapter_provenance", lambda **_kwargs: _decision()
    )
    agent = _box_agent_for(
        tmp_path,
        "overfull-hbox__abc1234",
        {"ALYSIS_CONFIG_SET": "skills_enabled=false;bad key=1;$(rm -rf /)=x;=novalue;novalue"},
    )

    cmds = agent._config_set_cmds()

    assert agent._config_set_passthrough() == {"skills_enabled": "false"}
    assert not any("rm -rf" in c or "bad key" in c for c in cmds)


def test_web_tools_switch_is_forwarded_into_the_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ALYSIS_WEB_TOOLS is the documented process-level kill switch; a host that
    # set it previously got the config default because it was not on the
    # forwarding allowlist, so "web off" campaigns ran with web on.
    monkeypatch.setattr(
        box_adapter_mod, "evaluate_adapter_provenance", lambda **_kwargs: _decision()
    )
    agent = _box_agent_for(tmp_path, "overfull-hbox__abc1234", {"ALYSIS_WEB_TOOLS": "off"})

    env = agent._container_env()
    fields = agent._run_manifest_fields()

    assert env["ALYSIS_WEB_TOOLS"] == "off"
    assert fields["web_tools_forwarded"] == "off"


def test_no_feature_switches_means_none_forwarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_TOOLS", raising=False)
    monkeypatch.delenv("ALYSIS_CONFIG_SET", raising=False)
    monkeypatch.setattr(
        box_adapter_mod, "evaluate_adapter_provenance", lambda **_kwargs: _decision()
    )
    agent = _box_agent_for(tmp_path, "overfull-hbox__abc1234", {})

    env = agent._container_env()
    fields = agent._run_manifest_fields()

    assert "ALYSIS_WEB_TOOLS" not in env
    assert fields["config_set_forwarded"] == {}
    assert fields["web_tools_forwarded"] is None
