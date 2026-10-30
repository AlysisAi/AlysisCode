from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_ROOT = _REPO_ROOT / "src"
if _SRC_ROOT.exists() and os.fspath(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(_SRC_ROOT))

try:
    from terminal_bench.agents.base_agent import AgentResult
    from terminal_bench.agents.installed_agents.abstract_installed_agent import (
        AbstractInstalledAgent,
    )
    from terminal_bench.terminal.models import TerminalCommand
    from terminal_bench.terminal.tmux_session import TmuxSession
except ModuleNotFoundError as exc:
    if exc.name and not exc.name.startswith("terminal_bench"):
        raise

    @dataclass
    class AgentResult:  # type: ignore[no-redef]
        total_input_tokens: int = 0
        total_output_tokens: int = 0
        failure_mode: object | None = None

    @dataclass
    class TerminalCommand:  # type: ignore[no-redef]
        command: str
        min_timeout_sec: float = 0.0
        max_timeout_sec: float = 180.0
        block: bool = False
        append_enter: bool = True

    class TmuxSession:  # type: ignore[no-redef]
        pass

    class AbstractInstalledAgent:  # type: ignore[no-redef]
        CONTAINER_AGENT_LOGS_PATH = "/agent-logs"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            _ = args
            self._version = kwargs.get("version")
            self._prompt_template = kwargs.get("prompt_template")

        @property
        def version(self) -> str | None:
            return self._version

        def _render_instruction(self, instruction: str) -> str:
            return instruction

        def perform_task(
            self,
            instruction: str,
            session: TmuxSession,
            logging_dir: Path | None = None,
        ) -> AgentResult:
            _ = logging_dir
            for command in self._run_agent_commands(self._render_instruction(instruction)):
                session.send_command(command)  # type: ignore[attr-defined]
            return AgentResult()


from alysis_code.managed_host_deadline import (  # noqa: E402
    DEFAULT_MANAGED_HOST_SHUTDOWN_RESERVE_SECONDS,
    MANAGED_HOST_DEADLINE_UNIX_ENV,
    ManagedHostDeadline,
    ManagedHostDeadlineError,
    managed_host_deadline_anchor_unix_seconds,
    resolve_managed_host_deadline,
)
from alysis_code.verification_contract import (  # noqa: E402
    VerificationCommandValidationStatus,
    build_verification_command_specs,
)

MANAGED_HOST_AGENT_TIMEOUT_KWARG = "managed_host_agent_timeout_sec"
MANAGED_HOST_SHUTDOWN_RESERVE_KWARG = "managed_host_shutdown_reserve_sec"
MANAGED_HOST_SHUTDOWN_RESERVE_ENV = "ALYSIS_MANAGED_HOST_SHUTDOWN_RESERVE_SEC"
MANAGED_HOST_DEADLINE_DIAGNOSTIC_FILENAME = "managed-host-deadline.json"
MANAGED_HOST_COMMAND_TIMEOUT_GRACE_SECONDS = 1.0
VERIFY_CMD_KWARG = "verify_cmd"
VERIFY_CMDS_KWARG = "verify_cmds"
VERIFY_CMD_ENV = "ALYSIS_VERIFY_CMD"


class AlysisSimpleAgent(AbstractInstalledAgent):
    """Run Alysis Code's one-shot CLI agent inside a Terminal-Bench task container."""

    @staticmethod
    def name() -> str:
        return "alysis-simple"

    def __init__(self, model_name: str | None = None, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._managed_host_agent_timeout_sec = kwargs.get(MANAGED_HOST_AGENT_TIMEOUT_KWARG)
        self._managed_host_agent_timeout_source = (
            f"agent_kwarg:{MANAGED_HOST_AGENT_TIMEOUT_KWARG}"
            if MANAGED_HOST_AGENT_TIMEOUT_KWARG in kwargs
            else "absent"
        )
        if MANAGED_HOST_SHUTDOWN_RESERVE_KWARG in kwargs:
            self._managed_host_shutdown_reserve_sec = kwargs[MANAGED_HOST_SHUTDOWN_RESERVE_KWARG]
            self._managed_host_shutdown_reserve_source = (
                f"agent_kwarg:{MANAGED_HOST_SHUTDOWN_RESERVE_KWARG}"
            )
        elif MANAGED_HOST_SHUTDOWN_RESERVE_ENV in os.environ:
            self._managed_host_shutdown_reserve_sec = os.environ[MANAGED_HOST_SHUTDOWN_RESERVE_ENV]
            self._managed_host_shutdown_reserve_source = (
                f"environment:{MANAGED_HOST_SHUTDOWN_RESERVE_ENV}"
            )
        else:
            self._managed_host_shutdown_reserve_sec = DEFAULT_MANAGED_HOST_SHUTDOWN_RESERVE_SECONDS
            self._managed_host_shutdown_reserve_source = "default"
        self._managed_host_started_at_monotonic: float | None = None
        self._managed_host_logging_dir: Path | None = None
        self._api_key = self._clean(
            kwargs.get("api_key")
            or os.environ.get("ALYSIS_API_KEY")
            or os.environ.get("DASHSCOPE_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
        self._model_name = self._clean(
            model_name
            or kwargs.get("model_name")
            or os.environ.get("ALYSIS_MODEL")
            or os.environ.get("OPENAI_MODEL")
            or "qwen3-coder-plus"
        )
        self._base_url = self._clean(
            kwargs.get("base_url")
            or os.environ.get("ALYSIS_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        )
        self._install_spec = self._clean(
            kwargs.get("install_spec") or os.environ.get("ALYSIS_INSTALL_SPEC") or "alysis-code"
        )
        self._max_steps = self._clean(
            kwargs.get("max_steps") or os.environ.get("ALYSIS_MAX_STEPS") or "100"
        )
        self._temperature = self._clean(
            kwargs.get("temperature") or os.environ.get("ALYSIS_TEMPERATURE") or "0.2"
        )
        self._llm_timeout_s = self._clean(
            kwargs.get("llm_timeout_s") or os.environ.get("ALYSIS_LLM_TIMEOUT_S") or "120"
        )
        self._web_search_mode = self._clean(
            kwargs.get("web_search_mode")
            or os.environ.get("ALYSIS_TBENCH_WEB_SEARCH_MODE")
            or "off"
        )
        self._verify_cmds, self._verify_cmd_source = self._resolve_host_verify_commands(kwargs)

    @staticmethod
    def _clean(value: object) -> str:
        return str(value or "").strip()

    @property
    def _env(self) -> dict[str, str]:
        if not self._api_key:
            raise ValueError(
                "Set ALYSIS_API_KEY, DASHSCOPE_API_KEY, OPENAI_API_KEY, "
                "or pass --agent-kwarg api_key=<key>."
            )
        env = {
            "PYTHONUNBUFFERED": "1",
            "ALYSIS_API_KEY": str(self._api_key),
            "ALYSIS_BASE_URL": str(self._base_url),
            "ALYSIS_INSTALL_SPEC": str(self._install_spec),
            "ALYSIS_LLM_TIMEOUT_S": self._llm_timeout_s,
            "ALYSIS_MODEL_METADATA_POLICY": "warn",
            "ALYSIS_MODEL": str(self._model_name),
            "ALYSIS_SHELL_SANDBOX_MODE": "off",
            "ALYSIS_TBENCH_WEB_SEARCH_MODE": str(self._web_search_mode),
            "ALYSIS_VERIFY_SANDBOX_MODE": "off",
        }
        # --strict-provenance refuses a run whose thinking setting is not
        # explicit; the host's setting is forwarded unchanged.
        thinking = self._clean(os.environ.get("ALYSIS_LLM_ENABLE_THINKING"))
        if thinking:
            env["ALYSIS_LLM_ENABLE_THINKING"] = thinking
        if len(self._verify_cmds) == 1:
            env[VERIFY_CMD_ENV] = self._verify_cmds[0]
        return env

    @property
    def _install_agent_script_path(self) -> Path:
        return Path(__file__).parent / "setup.sh"

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
    ) -> AgentResult:
        self._managed_host_started_at_monotonic = time.monotonic()
        self._managed_host_logging_dir = logging_dir
        try:
            self._validate_managed_host_deadline_before_setup()
            self._validate_host_verify_commands_before_setup()
            with tempfile.TemporaryDirectory(prefix="alysis-source-") as tmp:
                snapshot = Path(tmp) / "source"
                self._copy_source_snapshot(snapshot)
                session.copy_to_container(snapshot, container_dir="/installed-agent/alysis-source")
                return super().perform_task(instruction, session, logging_dir=logging_dir)
        finally:
            self._managed_host_started_at_monotonic = None
            self._managed_host_logging_dir = None

    def _copy_source_snapshot(self, snapshot: Path) -> None:
        repo_root = _REPO_ROOT
        snapshot.mkdir(parents=True, exist_ok=True)
        for filename in ("pyproject.toml", "README.md", "LICENSE", "NOTICE", "hatch_build.py"):
            source = repo_root / filename
            if source.exists():
                shutil.copy2(source, snapshot / filename)
        shutil.copytree(
            repo_root / "src",
            snapshot / "src",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )

    def _run_agent_commands(self, instruction: str) -> list[TerminalCommand]:
        self._validate_host_verify_commands_before_setup()
        deadline = self._resolve_managed_host_deadline_for_launch()
        command_parts = [
            "alysis",
            "run",
            "--path",
            ".",
            "--allow-broad-workspace",
            "--mode",
            "fullaccess",
            "--yes",
            "--no-log",
            # Streaming chosen on the command line, which --strict-provenance
            # requires: the token-plan endpoint closes a non-streaming request
            # at 900 s, and a stream survives long thinking.
            "--stream",
            "--strict-provenance",
            "--no-subagents",
            "--max-steps",
            self._max_steps,
            "--deadline-seconds",
            _format_seconds(deadline.alysis_invocation_deadline_seconds),
            "--require-deadline",
            "--temperature",
            self._temperature,
        ]
        for verify_cmd in self._verify_cmds:
            command_parts.extend(["--verify-cmd", verify_cmd])
        command_parts.extend(
            [
                "--api-key-env",
                "ALYSIS_API_KEY",
                "--base-url",
                str(self._base_url),
                "--model",
                str(self._model_name),
                "--",
                instruction,
            ]
        )
        anchor = managed_host_deadline_anchor_unix_seconds(
            deadline.alysis_invocation_deadline_seconds,
            existing_anchor=os.environ.get(MANAGED_HOST_DEADLINE_UNIX_ENV),
            now_unix_seconds=time.time(),
        )
        # A command-local assignment crosses the terminal transport without
        # exporting a launch deadline into later commands or saved settings.
        launch_env = f"{MANAGED_HOST_DEADLINE_UNIX_ENV}={anchor:.6f} "
        return [
            TerminalCommand(
                command=launch_env + " ".join(shlex.quote(part) for part in command_parts),
                min_timeout_sec=0.0,
                max_timeout_sec=(
                    deadline.host_remaining_timeout_seconds
                    + MANAGED_HOST_COMMAND_TIMEOUT_GRACE_SECONDS
                ),
                block=True,
                append_enter=True,
            )
        ]

    def _validate_managed_host_deadline_before_setup(self) -> None:
        try:
            resolve_managed_host_deadline(
                final_effective_host_agent_timeout_seconds=(self._managed_host_agent_timeout_sec),
                host_shutdown_reserve_seconds=self._managed_host_shutdown_reserve_sec,
                elapsed_before_launch_seconds=0.0,
                timeout_source=self._managed_host_agent_timeout_source,
                reserve_source=self._managed_host_shutdown_reserve_source,
            )
        except ManagedHostDeadlineError as exc:
            self._write_deadline_diagnostic(exc.record)
            raise ValueError(f"Managed-host deadline configuration error: {exc}") from exc

    def _resolve_managed_host_deadline_for_launch(self) -> ManagedHostDeadline:
        if self._managed_host_started_at_monotonic is None:
            elapsed_before_launch_seconds = 0.0
        else:
            elapsed_before_launch_seconds = (
                time.monotonic() - self._managed_host_started_at_monotonic
            )
        try:
            deadline = resolve_managed_host_deadline(
                final_effective_host_agent_timeout_seconds=(self._managed_host_agent_timeout_sec),
                host_shutdown_reserve_seconds=self._managed_host_shutdown_reserve_sec,
                elapsed_before_launch_seconds=elapsed_before_launch_seconds,
                timeout_source=self._managed_host_agent_timeout_source,
                reserve_source=self._managed_host_shutdown_reserve_source,
            )
        except ManagedHostDeadlineError as exc:
            self._write_deadline_diagnostic(exc.record)
            raise ValueError(f"Managed-host deadline configuration error: {exc}") from exc

        record = deadline.diagnostic_record()
        record["terminal_command_timeout_seconds"] = round(
            deadline.host_remaining_timeout_seconds + MANAGED_HOST_COMMAND_TIMEOUT_GRACE_SECONDS,
            6,
        )
        record.update(self._host_verifier_diagnostic_payload())
        self._write_deadline_diagnostic(record)
        return deadline

    def _resolve_host_verify_commands(self, kwargs: dict[str, Any]) -> tuple[tuple[str, ...], str]:
        if VERIFY_CMD_KWARG in kwargs and VERIFY_CMDS_KWARG in kwargs:
            raise ValueError(
                "Managed-host verifier configuration error: verify_cmd and verify_cmds "
                "are mutually exclusive"
            )
        if VERIFY_CMDS_KWARG in kwargs:
            return (
                self._normalize_host_verify_commands(
                    kwargs.get(VERIFY_CMDS_KWARG),
                    explicit=True,
                ),
                f"agent_kwarg:{VERIFY_CMDS_KWARG}",
            )
        if VERIFY_CMD_KWARG in kwargs:
            return (
                self._normalize_host_verify_commands(
                    kwargs.get(VERIFY_CMD_KWARG),
                    explicit=True,
                ),
                f"agent_kwarg:{VERIFY_CMD_KWARG}",
            )
        if VERIFY_CMD_ENV in os.environ:
            return (
                self._normalize_host_verify_commands(
                    os.environ.get(VERIFY_CMD_ENV),
                    explicit=False,
                ),
                f"environment:{VERIFY_CMD_ENV}",
            )
        return tuple(), "unavailable"

    @staticmethod
    def _normalize_host_verify_commands(raw: object, *, explicit: bool) -> tuple[str, ...]:
        if raw is None:
            values: list[object] = []
        elif isinstance(raw, str):
            values = [raw]
        elif isinstance(raw, (list, tuple)):
            values = list(raw)
        elif isinstance(raw, set):
            raise ValueError(
                "Managed-host verifier configuration error: verifier commands must be "
                "an ordered sequence, not a set"
            )
        else:
            raise ValueError(
                "Managed-host verifier configuration error: verifier commands must be "
                "a string or ordered sequence"
            )
        commands: list[str] = []
        for item in values:
            text = str(item).strip()
            if not text:
                if explicit:
                    raise ValueError(
                        "Managed-host verifier configuration error: verifier command is empty"
                    )
                continue
            commands.append(text)
        if explicit and not commands:
            raise ValueError("Managed-host verifier configuration error: verifier is empty")
        return tuple(commands)

    def _host_verifier_diagnostic_payload(self) -> dict[str, Any]:
        command_hashes = [
            hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]
            for command in self._verify_cmds
        ]
        return {
            "host_verifier_status": "provided" if self._verify_cmds else "unavailable",
            "host_verifier_source": self._verify_cmd_source,
            "host_verifier_count": len(self._verify_cmds),
            "host_verifier_command_hashes": command_hashes,
        }

    def _validate_host_verify_commands_before_setup(self) -> None:
        if not self._verify_cmds:
            return
        specs = build_verification_command_specs(
            self._verify_cmds,
            source="cli.verify_cmd",
            contract_type="explicit_override",
        )
        invalid_reasons = [
            spec.rejection_reason or "invalid_verification_command"
            for spec in specs
            if spec.validation_status == VerificationCommandValidationStatus.INVALID
        ]
        if not invalid_reasons:
            return
        record = {
            "schema_version": 1,
            "status": "blocked",
            "validation_error": "invalid_host_verifier",
            "host_verifier_rejection_reasons": sorted(set(invalid_reasons)),
            **self._host_verifier_diagnostic_payload(),
        }
        self._write_deadline_diagnostic(record)
        raise ValueError(
            "Managed-host verifier configuration error: " + ", ".join(sorted(set(invalid_reasons)))
        )

    def _write_deadline_diagnostic(self, record: dict[str, Any]) -> None:
        if self._managed_host_logging_dir is None:
            return
        self._managed_host_logging_dir.mkdir(parents=True, exist_ok=True)
        path = self._managed_host_logging_dir / MANAGED_HOST_DEADLINE_DIAGNOSTIC_FILENAME
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _format_seconds(value: float) -> str:
    formatted = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return formatted or "0"
