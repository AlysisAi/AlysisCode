from __future__ import annotations

import asyncio
import math
import os
import shlex
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_ROOT = _REPO_ROOT / "src"
if _SRC_ROOT.exists() and os.fspath(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(_SRC_ROOT))

from alysis_code.managed_host_deadline import (  # noqa: E402
    MANAGED_HOST_DEADLINE_UNIX_ENV,
    managed_host_deadline_anchor_unix_seconds,
    resolve_managed_host_deadline,
)
from alysis_code.run_outcome import (  # noqa: E402
    AGENT_FAILURE_EXIT_CODE,
    SUCCESS_EXIT_CODE,
    extract_process_exit_code,
    run_outcome_metadata,
)
from scripts.benchmarks.terminal_bench.adapter_outcome import (  # noqa: E402
    outcome_emit_command,
    outcome_metadata,
)
from scripts.benchmarks.terminal_bench.session_mirror import compose_run_command  # noqa: E402

try:
    from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
    from harbor.environments.base import BaseEnvironment
    from harbor.models.agent.context import AgentContext
    from harbor.models.trial.paths import EnvironmentPaths
except ModuleNotFoundError as exc:
    if exc.name and not exc.name.startswith("harbor"):
        raise

    class AgentContext:  # type: ignore[no-redef]
        def __init__(self) -> None:
            self.metadata: dict[str, Any] | None = None

    class BaseEnvironment:  # type: ignore[no-redef]
        pass

    class _EnvironmentPaths:  # type: ignore[no-redef]
        agent_dir = Path("/logs/agent")

    EnvironmentPaths = _EnvironmentPaths()  # type: ignore[assignment]

    def with_prompt_template(fn: Any) -> Any:  # type: ignore[no-redef]
        return fn

    class BaseInstalledAgent:  # type: ignore[no-redef]
        def __init__(
            self,
            logs_dir: Path | str = Path("."),
            model_name: str | None = None,
            version: str | None = None,
            extra_env: dict[str, str] | None = None,
            *args: Any,
            **kwargs: Any,
        ) -> None:
            _ = args, kwargs
            self.logs_dir = Path(logs_dir)
            self.model_name = model_name
            self._version = version
            self._extra_env = dict(extra_env or {})

        def version(self) -> str | None:
            return self._version

        def _get_env(self, key: str) -> str | None:
            if key in self._extra_env:
                return self._extra_env[key]
            return os.environ.get(key)


DEFAULT_INSTALL_SPEC = "alysis-code"
DEFAULT_MAX_STEPS = "1000"
DEFAULT_TEMPERATURE = "0.2"
DEFAULT_LLM_TIMEOUT_S = "240"
DEFAULT_SHUTDOWN_RESERVE_SEC = "120"
DEFAULT_SUBAGENTS = True
SETUP_TIMEOUT_SEC = 1800
AGENT_ARTIFACT_DIR = "/logs/agent/alysis"


class AlysisHarborAgent(BaseInstalledAgent):
    """Run Alysis Code as a Harbor installed agent for Terminal-Bench 2.x."""

    @staticmethod
    def name() -> str:
        return "alysis-harbor"

    def __init__(
        self,
        logs_dir: Path | str = Path("."),
        model_name: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        install_spec: str | None = None,
        max_steps: int | str | None = None,
        temperature: float | str | None = None,
        llm_timeout_s: float | str | None = None,
        command_timeout_sec: float | str | None = None,
        shutdown_reserve_sec: float | str | None = None,
        subagents: bool | str | None = None,
        tbench_web_search_mode: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self._api_key_arg = _clean(api_key)
        self._base_url_arg = _clean(base_url)
        self._install_spec_arg = _clean(install_spec)
        self._max_steps_arg = _clean(max_steps)
        self._temperature_arg = _clean(temperature)
        self._llm_timeout_s_arg = _clean(llm_timeout_s)
        self._command_timeout_arg = _clean(command_timeout_sec)
        self._shutdown_reserve_arg = _clean(shutdown_reserve_sec)
        self._subagents_arg = subagents
        self._web_search_mode_arg = _clean(tbench_web_search_mode)
        super().__init__(Path(logs_dir), *args, model_name=model_name, **kwargs)

        self._model_name = _first_clean(
            model_name,
            self._get_env("ALYSIS_MODEL"),
            self._get_env("OPENAI_MODEL"),
        )
        if not self._model_name:
            raise ValueError(
                "Set ALYSIS_MODEL, OPENAI_MODEL, or pass --agent-kwarg model_name=<model>."
            )
        self._base_url = _first_clean(
            self._base_url_arg,
            self._get_env("ALYSIS_BASE_URL"),
            self._get_env("OPENAI_BASE_URL"),
        )
        if not self._base_url:
            raise ValueError(
                "Set ALYSIS_BASE_URL, OPENAI_BASE_URL, or pass --agent-kwarg "
                "base_url=<OpenAI-compatible endpoint>."
            )
        self._install_spec = _first_clean(
            self._install_spec_arg,
            self._get_env("ALYSIS_INSTALL_SPEC"),
            DEFAULT_INSTALL_SPEC,
        )
        self._max_steps = _first_clean(
            self._max_steps_arg,
            self._get_env("ALYSIS_MAX_STEPS"),
            DEFAULT_MAX_STEPS,
        )
        self._temperature = _first_clean(
            self._temperature_arg,
            self._get_env("ALYSIS_TEMPERATURE"),
            DEFAULT_TEMPERATURE,
        )
        self._llm_timeout_s = _first_clean(
            self._llm_timeout_s_arg,
            self._get_env("ALYSIS_LLM_TIMEOUT_S"),
            DEFAULT_LLM_TIMEOUT_S,
        )
        self._command_timeout_sec = _positive_float(
            _first_clean(
                self._command_timeout_arg,
                self._get_env("ALYSIS_TBENCH_COMMAND_TIMEOUT_SEC"),
                self._get_env("TB_AGENT_TIMEOUT_SEC"),
            )
        )
        self._shutdown_reserve_sec = _positive_float(
            _first_clean(
                self._shutdown_reserve_arg,
                self._get_env("ALYSIS_MANAGED_HOST_SHUTDOWN_RESERVE_SEC"),
                DEFAULT_SHUTDOWN_RESERVE_SEC,
            )
        )
        self._subagents = _bool_value(
            self._subagents_arg,
            env_value=self._get_env("ALYSIS_SUBAGENTS"),
            default=DEFAULT_SUBAGENTS,
        )
        self._web_search_mode = _first_clean(
            self._web_search_mode_arg,
            self._get_env("ALYSIS_TBENCH_WEB_SEARCH_MODE"),
            "off",
        )

    def get_version_command(self) -> str | None:
        return "alysis --version"

    def version(self) -> str | None:
        return getattr(self, "_version", None)

    async def install(self, environment: BaseEnvironment) -> None:
        with tempfile.TemporaryDirectory(prefix="alysis-harbor-source-") as tmp:
            snapshot = Path(tmp) / "source"
            _copy_source_snapshot(snapshot)
            await environment.upload_dir(snapshot, "/installed-agent/alysis-source")

        await self.exec_as_root(  # type: ignore[attr-defined]
            environment,
            command=(
                "chmod +x /installed-agent/alysis-source/scripts/benchmarks/terminal_bench/setup.sh "
                "&& /installed-agent/alysis-source/scripts/benchmarks/terminal_bench/setup.sh"
            ),
            env=self._install_env(),
            timeout_sec=SETUP_TIMEOUT_SEC,
        )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        started = time.monotonic()
        command = self._build_run_command(instruction)
        env = self._runtime_env()
        launch_id = uuid.uuid4().hex
        outcome_path = f"{AGENT_ARTIFACT_DIR}/host-outcome-{launch_id}.json"
        outcome_marker = f"ALYSIS_TASK_OUTCOME_{launch_id}="
        env["ALYSIS_TASK_OUTCOME_PATH"] = outcome_path
        command = compose_run_command(
            command,
            mirror_clause=outcome_emit_command(
                path=outcome_path,
                marker=outcome_marker,
            ),
        )
        timeout = _ceil_timeout(self._command_timeout_sec)
        run_error: Exception | None = None
        process_result = None
        try:
            try:
                deadline_seconds = self._deadline_seconds()
                if deadline_seconds is not None:
                    env[MANAGED_HOST_DEADLINE_UNIX_ENV] = _format_seconds(
                        managed_host_deadline_anchor_unix_seconds(
                            deadline_seconds,
                            existing_anchor=self._get_env(MANAGED_HOST_DEADLINE_UNIX_ENV),
                            now_unix_seconds=time.time(),
                        )
                    )
                process_result = await asyncio.wait_for(
                    self.exec_as_agent(  # type: ignore[attr-defined]
                        environment,
                        command=command,
                        env=env,
                        timeout_sec=timeout,
                    ),
                    timeout=timeout,
                )
            except Exception as exc:
                run_error = exc
                raise
        finally:
            try:
                remaining = max(
                    0.0, float(self._command_timeout_sec or 0) - (time.monotonic() - started)
                )
                if remaining > 0:
                    await asyncio.wait_for(
                        self._copy_runtime_artifacts(environment), timeout=remaining
                    )
            finally:
                exit_code = (
                    extract_process_exit_code(run_error)
                    if run_error is not None
                    else SUCCESS_EXIT_CODE
                )
                context.metadata = {
                    **(context.metadata or {}),
                    "alysis_model": self._model_name,
                    "alysis_base_url": self._base_url,
                    "alysis_max_steps": int(float(self._max_steps)),
                    "alysis_subagents": self._subagents,
                    "alysis_artifacts_hint": AGENT_ARTIFACT_DIR,
                    **run_outcome_metadata(
                        exit_code if exit_code is not None else AGENT_FAILURE_EXIT_CODE
                    ),
                    **outcome_metadata(run_error or process_result, marker=outcome_marker),
                }

    def _build_run_command(self, instruction: str) -> str:
        parts = [
            "alysis",
            "run",
            "--path",
            ".",
            "--allow-broad-workspace",
            "--mode",
            "fullaccess",
            "--yes",
            # Streaming chosen on the command line, which --strict-provenance
            # requires: the token-plan endpoint closes a non-streaming request
            # at 900 s, and a stream survives long thinking.
            "--stream",
            "--strict-provenance",
            "--max-steps",
            self._max_steps,
            "--temperature",
            self._temperature,
        ]
        deadline = self._deadline_seconds()
        if deadline is not None:
            parts.extend(["--deadline-seconds", _format_seconds(deadline), "--require-deadline"])
        if self._subagents:
            parts.append("--subagents")
        else:
            parts.append("--no-subagents")
        parts.extend(
            [
                "--api-key-env",
                "ALYSIS_API_KEY",
                "--base-url",
                self._base_url,
                "--model",
                self._model_name,
                "--",
                instruction,
            ]
        )
        return " ".join(shlex.quote(str(part)) for part in parts)

    def _deadline_seconds(self) -> float | None:
        return resolve_managed_host_deadline(
            final_effective_host_agent_timeout_seconds=self._command_timeout_sec,
            host_shutdown_reserve_seconds=self._shutdown_reserve_sec or 0,
            timeout_source="host.command_timeout_sec",
        ).alysis_invocation_deadline_seconds

    def _install_env(self) -> dict[str, str]:
        return self._shared_env()

    def _runtime_env(self) -> dict[str, str]:
        api_key = _first_clean(
            self._api_key_arg,
            self._get_env("ALYSIS_API_KEY"),
            self._get_env("OPENROUTER_API_KEY"),
            self._get_env("DASHSCOPE_API_KEY"),
            self._get_env("OPENAI_API_KEY"),
        )
        if not api_key:
            raise ValueError(
                "Set ALYSIS_API_KEY, OPENROUTER_API_KEY, DASHSCOPE_API_KEY, OPENAI_API_KEY, "
                "or pass --agent-kwarg api_key=<key>."
            )
        env = self._shared_env()
        env.update(
            {
                "ALYSIS_API_KEY": api_key,
                "ALYSIS_RUN_DEADLINE_SECONDS": _format_seconds(self._deadline_seconds())
                if self._deadline_seconds() is not None
                else "",
            }
        )
        return env

    def _shared_env(self) -> dict[str, str]:
        env = {
            "PYTHONUNBUFFERED": "1",
            "ALYSIS_BASE_URL": self._base_url,
            "ALYSIS_INSTALL_SPEC": self._install_spec,
            "ALYSIS_LLM_TIMEOUT_S": self._llm_timeout_s,
            "ALYSIS_MODEL": self._model_name,
            "ALYSIS_MODEL_METADATA_POLICY": "warn",
            "ALYSIS_SHELL_SANDBOX_MODE": "off",
            "ALYSIS_TBENCH_WEB_SEARCH_MODE": self._web_search_mode,
            "ALYSIS_VERIFY_SANDBOX_MODE": "off",
        }
        # --strict-provenance refuses a run whose thinking setting is not
        # explicit; the host's setting is forwarded unchanged.
        thinking = _first_clean(self._get_env("ALYSIS_LLM_ENABLE_THINKING"))
        if thinking:
            env["ALYSIS_LLM_ENABLE_THINKING"] = thinking
        return env

    async def _copy_runtime_artifacts(self, environment: BaseEnvironment) -> None:
        command = (
            f"mkdir -p {shlex.quote(AGENT_ARTIFACT_DIR)}\n"
            "if [ -d .alysis ]; then\n"
            f"  rm -rf {shlex.quote(AGENT_ARTIFACT_DIR)}/runtime\n"
            f"  cp -R .alysis {shlex.quote(AGENT_ARTIFACT_DIR)}/runtime\n"
            "fi\n"
            f"if [ -f {shlex.quote(str(EnvironmentPaths.agent_dir))}/alysis.out ]; then :; fi"
        )
        try:
            await self.exec_as_agent(environment, command=command)  # type: ignore[attr-defined]
        except Exception:
            return


def _copy_source_snapshot(snapshot: Path) -> None:
    snapshot.mkdir(parents=True, exist_ok=True)
    for filename in ("pyproject.toml", "README.md", "LICENSE", "NOTICE", "hatch_build.py"):
        source = _REPO_ROOT / filename
        if source.exists():
            shutil.copy2(source, snapshot / filename)
    shutil.copytree(
        _REPO_ROOT / "src",
        snapshot / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    bench_dir = snapshot / "scripts" / "benchmarks" / "terminal_bench"
    bench_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__).with_name("setup.sh"), bench_dir / "setup.sh")


def _clean(value: object) -> str:
    return str(value or "").strip()


def _first_clean(*values: object) -> str:
    for value in values:
        cleaned = _clean(value)
        if cleaned:
            return cleaned
    return ""


def _positive_float(value: str) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _ceil_timeout(value: float | None) -> int | None:
    if value is None:
        return None
    return max(1, int(math.ceil(value)))


def _format_seconds(value: float | None) -> str:
    if value is None:
        return ""
    formatted = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return formatted or "0"


def _bool_value(value: object, *, env_value: object, default: bool) -> bool:
    if value is None:
        value = env_value
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default
