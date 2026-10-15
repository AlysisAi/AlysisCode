"""
Harbor installed-agent adapter used by the benchmark runner box.

This adapter installs a prebuilt Alysis Code wheel into each Terminal-Bench task
container, then runs ``alysis run`` non-interactively. Keep this file in the
repo so the runner does not depend on an unversioned local rewrite.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sys
import time
import uuid
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_ROOT = _REPO_ROOT / "src"
if _SRC_ROOT.exists() and os.fspath(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(_SRC_ROOT))

from alysis_code.execution_deadline import validate_deadline_seconds  # noqa: E402
from alysis_code.managed_host_deadline import (  # noqa: E402
    MANAGED_HOST_DEADLINE_UNIX_ENV,
    ManagedHostDeadlineError,
    managed_host_deadline_anchor_unix_seconds,
    resolve_managed_host_deadline,
)
from alysis_code.run_outcome import (  # noqa: E402
    AGENT_FAILURE_EXIT_CODE,
    SUCCESS_EXIT_CODE,
    extract_process_exit_code,
    run_outcome_metadata,
)


def _load_sibling(module_name: str, filename: str) -> Any:
    """Load a sibling helper module when there is no package context.

    Harbor imports this file as ``scripts.benchmarks.terminal_bench.box_harbor_agent``
    via ``--agent-import-path``, where the relative imports below work. Loading
    it as a bare file has to keep working too, so each helper falls back to a
    by-path load rather than failing the whole adapter.
    """
    import importlib.util as _importlib_util

    spec = _importlib_util.spec_from_file_location(module_name, Path(__file__).with_name(filename))
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {filename}")
    module = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    from .adapter_outcome import outcome_emit_command, outcome_metadata
except ImportError:
    _outcome = _load_sibling("_alysis_adapter_outcome", "adapter_outcome.py")
    outcome_emit_command = _outcome.outcome_emit_command
    outcome_metadata = _outcome.outcome_metadata


try:
    from .adapter_provenance import (
        PROVENANCE_GUARD_ENV,
        ProvenanceError,
        evaluate_adapter_provenance,
        guard_enabled,
    )
except ImportError:
    _prov = _load_sibling("_tb_adapter_provenance", "adapter_provenance.py")
    PROVENANCE_GUARD_ENV = _prov.PROVENANCE_GUARD_ENV
    ProvenanceError = _prov.ProvenanceError
    evaluate_adapter_provenance = _prov.evaluate_adapter_provenance
    guard_enabled = _prov.guard_enabled

try:
    from .session_mirror import (
        MIRROR_ENV_VAR,
        agent_pipeline_command,
        compose_run_command,
        mirror_enabled,
        session_mirror_clause,
    )
except ImportError:
    _mirror = _load_sibling("_tb_session_mirror", "session_mirror.py")
    MIRROR_ENV_VAR = _mirror.MIRROR_ENV_VAR
    agent_pipeline_command = _mirror.agent_pipeline_command
    compose_run_command = _mirror.compose_run_command
    mirror_enabled = _mirror.mirror_enabled
    session_mirror_clause = _mirror.session_mirror_clause

try:
    from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
    from harbor.environments.base import BaseEnvironment
    from harbor.models.agent.context import AgentContext
except ModuleNotFoundError as exc:
    if exc.name and not exc.name.startswith("harbor"):
        raise

    class AgentContext:  # type: ignore[no-redef]
        def __init__(self) -> None:
            self.metadata: dict[str, Any] | None = None

    class BaseEnvironment:  # type: ignore[no-redef]
        pass

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


_WHEEL_DIR = "/tmp/alysis-agent"
_CONFIG_DIR = "/tmp/alysis-cfg"
_ART_DIR = "/logs/artifacts"
_SESSION_DIR = f"{_ART_DIR}/alysis-session"
_CRASH_LOG = f"{_ART_DIR}/alysis-crash.jsonl"
# Retain the existing global ceiling and clean-stop fraction. The host supplies
# each job's allowance; no task identity participates in this calculation.
_MAX_RUN_BUDGET_SECONDS = 10800.0
_HOST_BUDGET_FRACTION = 0.85
_SETUP_DIR = "/installed-agent/alysis-source/scripts/benchmarks/terminal_bench"
_SETUP_SCRIPT = f"{_SETUP_DIR}/setup.sh"
_SETUP_TIMEOUT_SEC = 1800
# Written host-side next to the trial's logs. context.metadata is the primary
# record, but it only survives if Harbor completes the trial; a run refused at
# startup, or one that dies mid-task, still leaves this file behind.
_PROVENANCE_FILENAME = "alysis-provenance.json"
# Host-side passthrough for `alysis config set` inside the container:
# ``ALYSIS_CONFIG_SET="skills_enabled=false;skills_auto_invoke=false"``.
# The adapter only knew a fixed handful of config keys, so a campaign had no way
# to switch off a feature the wheel enables by default -- bundled skills with
# semantic auto-selection shipped in 0.14.x and, being inside the wheel, are
# discovered in every task container, adding a skills block to the system
# prompt and a router LLM call at session start that the baseline never had.
# Every forwarded pair is recorded in the manifest so the prompt surface a
# task actually ran under is provable, not assumed.
_CONFIG_SET_ENV = "ALYSIS_CONFIG_SET"
_CONFIG_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class AlysisAgent(BaseInstalledAgent):
    """Installed-agent wrapper around ``alysis run``."""

    SUPPORTS_ATIF = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._host_timeout = kwargs.get("managed_host_agent_timeout_sec")
        self._host_reserve = kwargs.get("managed_host_shutdown_reserve_sec")
        self._host_started: float | None = None
        self._host_metadata: dict[str, Any] = {}
        self._deadline_record: dict[str, Any] = {"status": "pending_host_metadata"}
        # Resolved here so every later caller reads one answer, taken before
        # anything could have changed underneath it. Only resolved, though:
        # constructing an adapter is not starting a run, and a constructor
        # that refuses cannot be built for inspection, tests, or tooling.
        # install() is where the refusal belongs -- see _enforce_provenance.
        self._provenance = self._resolve_provenance()

    def _resolve_provenance(self) -> Any:
        """Compare the wheel under test against the checkout this file came from.

        ``__file__`` is the input on purpose: the defect being guarded against
        is an adapter imported from somewhere other than where the operator
        believes it is, and any configured root would be describing the belief
        rather than the fact.
        """
        return evaluate_adapter_provenance(
            adapter_file=__file__,
            wheel_path=self._get_env("ALYSIS_WHEEL") or "",
            getenv=self._get_env,
        )

    def _provenance_guard_enabled(self) -> bool:
        return guard_enabled(self._get_env(PROVENANCE_GUARD_ENV))

    def _enforce_provenance(self) -> None:
        """Refuse a run whose wheel and adapter checkout disagree.

        Called from ``install``, which is where the wheel actually enters the
        trial and where the existing clean-build gate already runs. That makes
        it both the earliest point the check can mean anything and the only
        point it needs to happen: Harbor installs before it runs, so a refusal
        here stops the campaign before any task executes, and the refusal lands
        in the first trial's log rather than after a few hundred tasks have
        already produced unattributable scores.

        Skipped when no wheel is configured at all: ``install`` already fails
        with a specific message for that, and replacing it with a provenance
        refusal would send the operator looking for the wrong problem.
        """
        if not self._get_env("ALYSIS_WHEEL"):
            return
        decision = self._provenance
        if not decision.failed:
            return
        codes = ",".join(decision.failure_codes)
        if not self._provenance_guard_enabled():
            print(f"alysis_provenance guard_disabled codes={codes}")
            return
        if decision.overridden:
            # Recorded, not swallowed: the disagreement and the reason both go
            # into every artifact this run produces.
            print(f"alysis_provenance overridden codes={codes} reason={decision.override_reason}")
            return
        raise ProvenanceError(decision.message())

    def _provenance_payload(self) -> dict[str, Any]:
        payload = dict(self._provenance.payload())
        payload["guard_enabled"] = self._provenance_guard_enabled()
        return {"alysis_provenance": payload}

    def _llm_timeout_fields(self) -> dict[str, Any]:
        """What this adapter did about the LLM timeout, stated host-side.

        The container-side truth lives in each session's ``config_snapshot``
        (``effective.llm_timeout_s``); this is the delivery half of the story.
        Recorded because a campaign's run notes once said 240 while every
        session dumped the config default of 60.0, and reconstructing which
        number was real took a forensic pass over container environments. The
        manifest now states what was forwarded, so the two artifacts can be
        checked against each other instead of against assumptions.
        """
        forwarded = self._get_env("ALYSIS_LLM_TIMEOUT_S")
        if forwarded:
            return {
                "llm_timeout_forwarded_s": forwarded,
                "llm_timeout_delivery": "host_env",
            }
        return {
            "llm_timeout_forwarded_s": None,
            "llm_timeout_delivery": "container_default",
        }

    def _deadline_resolution(self) -> dict[str, Any]:
        """Resolve host authority at launch, subtracting all setup elapsed time.

        Context metadata is provided by the host after installation. A total
        timeout kwarg/environment value starts at run entry; remaining_seconds
        starts when that metadata is consumed. An absolute Unix deadline also
        includes time spent before run entry. Legacy budget flags are ceilings
        only and never substitute for missing host deadline metadata.
        """
        metadata = self._host_metadata
        raw_timeout = metadata.get("remaining_seconds", metadata.get("timeout_seconds"))
        source = "context.managed_host_deadline"
        if "deadline_unix_seconds" in metadata:
            if "_initial_remaining_seconds" not in metadata:
                try:
                    absolute = validate_deadline_seconds(
                        metadata["deadline_unix_seconds"], key="deadline_unix_seconds"
                    )
                except (ValueError, TypeError) as exc:
                    self._deadline_record = {
                        "status": "blocked",
                        "validation_error": "invalid_host_deadline_metadata",
                        "timeout_source": source,
                    }
                    raise ValueError(f"Managed-host deadline configuration error: {exc}") from exc
                metadata["_initial_remaining_seconds"] = absolute - time.time()
            raw_timeout = metadata["_initial_remaining_seconds"]
        if raw_timeout is None:
            raw_timeout = self._host_timeout
            source = "agent_kwarg:managed_host_agent_timeout_sec"
        if raw_timeout is None:
            raw_timeout = self._get_env("ALYSIS_MANAGED_HOST_AGENT_TIMEOUT_SEC")
            source = "environment:ALYSIS_MANAGED_HOST_AGENT_TIMEOUT_SEC"
        if raw_timeout is None:
            source = "absent"
        elapsed = (
            0.0 if self._host_started is None else max(0.0, time.monotonic() - self._host_started)
        )
        try:
            # Validate with the shared resolver before calculating a fraction.
            base = resolve_managed_host_deadline(
                final_effective_host_agent_timeout_seconds=raw_timeout,
                host_shutdown_reserve_seconds=0,
                elapsed_before_launch_seconds=elapsed,
                timeout_source=source,
            )
            host_seconds = base.final_effective_host_agent_timeout_seconds
            reserve = metadata.get("shutdown_reserve_seconds", self._host_reserve)
            if reserve is None:
                reserve = self._get_env("ALYSIS_MANAGED_HOST_SHUTDOWN_RESERVE_SEC") or 30.0
            reserve = resolve_managed_host_deadline(
                final_effective_host_agent_timeout_seconds=host_seconds,
                host_shutdown_reserve_seconds=reserve,
                elapsed_before_launch_seconds=elapsed,
                timeout_source=source,
            ).host_shutdown_reserve_seconds
            # Preserve the established conservative headroom for every workload.
            reserve = max(float(reserve), host_seconds * (1.0 - _HOST_BUDGET_FRACTION))
            ceiling = min(_MAX_RUN_BUDGET_SECONDS, host_seconds - reserve)
            for key in ("ALYSIS_RUN_BUDGET_SECONDS", "ALYSIS_DEADLINE_SECONDS"):
                value = self._get_env(key)
                if value is not None:
                    ceiling = min(ceiling, validate_deadline_seconds(value, key=key))
            deadline = resolve_managed_host_deadline(
                final_effective_host_agent_timeout_seconds=host_seconds,
                host_shutdown_reserve_seconds=host_seconds - ceiling,
                elapsed_before_launch_seconds=elapsed,
                timeout_source=source,
                reserve_source="conservative_host_reserve_and_ceiling",
            )
        except (ManagedHostDeadlineError, ValueError, TypeError) as exc:
            self._deadline_record = getattr(
                exc,
                "record",
                {
                    "status": "blocked",
                    "validation_error": "invalid_host_deadline_metadata",
                    "timeout_source": source,
                },
            )
            raise ValueError(f"Managed-host deadline configuration error: {exc}") from exc
        seconds = deadline.alysis_invocation_deadline_seconds
        self._deadline_record = {
            **deadline.diagnostic_record(),
            "seconds": seconds,
            "budget_seconds": seconds,
            "budget_source": "host_metadata",
            "budget_table_hit": False,
            "deadline_seconds_forwarded": seconds,
            "deadline_source": source,
            "host_remaining_timeout_seconds": deadline.host_remaining_timeout_seconds,
        }
        return self._deadline_record

    def _config_set_passthrough(self) -> dict[str, str]:
        """``ALYSIS_CONFIG_SET`` parsed into ``{key: value}``, in host order.

        ``key=value`` pairs separated by ``;``. A key must look like a config
        key (``skills_enabled``); anything else is dropped rather than turned
        into a shell command, because this string is interpolated into
        ``alysis config set`` inside the container. Values are shell-quoted at
        the call site. Later duplicates win, matching ``config set`` semantics.
        """
        raw = self._get_env(_CONFIG_SET_ENV) or ""
        pairs: dict[str, str] = {}
        for item in raw.split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            key, value = item.split("=", 1)
            key = key.strip()
            value = value.strip()
            if _CONFIG_KEY_RE.match(key) and value:
                pairs[key] = value
        return pairs

    def _feature_switch_fields(self) -> dict[str, Any]:
        """What the host did about feature switches, stated host-side.

        The container-side truth is each session's ``config_snapshot``; this is
        the delivery half, so the prompt surface a task ran under (skills block
        present or not, web tools registered or not) can be checked against the
        campaign's intent instead of inferred from behaviour afterwards.
        """
        return {
            "config_set_forwarded": self._config_set_passthrough(),
            "web_tools_forwarded": self._get_env("ALYSIS_WEB_TOOLS") or None,
        }

    def _run_manifest_fields(self) -> dict[str, Any]:
        """Provenance and budget facts, for both the success and failure paths.

        Emitted on failure too: a trial that crashed is exactly the one whose
        provenance someone will want to check afterwards.
        """
        return {
            **self._provenance_payload(),
            **{k: v for k, v in self._deadline_record.items() if k != "seconds"},
            "managed_host_deadline": {
                k: v for k, v in self._deadline_record.items() if k != "seconds"
            },
            **self._llm_timeout_fields(),
            **self._feature_switch_fields(),
        }

    def _write_provenance_artifact(self) -> None:
        """Drop the provenance record beside the trial's logs, best effort.

        Never allowed to break a run: an unwritable logs directory is a reason
        to lose the file, not the campaign. ``context.metadata`` carries the
        same record, so nothing is lost that matters.

        A ``logs_dir`` that resolves to the working directory means Harbor
        never configured one -- it is the default -- and a benchmark adapter
        has no business dropping files into whatever directory the operator
        happened to launch from.
        """
        logs_dir = getattr(self, "logs_dir", None)
        if not logs_dir:
            return
        try:
            target = Path(logs_dir).resolve()
            if target == Path.cwd().resolve():
                return
            target.mkdir(parents=True, exist_ok=True)
            (target / _PROVENANCE_FILENAME).write_text(
                json.dumps(self._run_manifest_fields(), indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError):
            return

    @staticmethod
    def name() -> str:
        return "alysis"

    def version(self) -> str | None:
        return self._get_env("ALYSIS_BENCH_VERSION") or "bench"

    def _host_wheel_path(self) -> str:
        wheel = self._get_env("ALYSIS_WHEEL")
        if not wheel:
            raise RuntimeError(
                "ALYSIS_WHEEL is not set. Point it at the Alysis Code wheel built "
                "from the benchmark branch."
            )
        if not Path(wheel).is_file():
            raise RuntimeError(f"ALYSIS_WHEEL file not found: {wheel}")
        return wheel

    def _host_setup_script_path(self) -> str:
        setup = self._get_env("ALYSIS_TBENCH_SETUP_SH")
        if setup:
            path = Path(setup)
        else:
            path = Path(__file__).with_name("setup.sh")
        if not path.is_file():
            raise RuntimeError(f"Terminal-Bench setup.sh not found: {path}")
        return path.as_posix()

    def _container_wheel_path(self) -> str:
        return f"{_WHEEL_DIR}/{Path(self._host_wheel_path()).name}"

    def _model(self) -> str:
        model = self._get_env("ALYSIS_MODEL")
        if model:
            return model
        mn = getattr(self, "model_name", None)
        if mn:
            return mn.split("/", 1)[-1]
        raise RuntimeError("ALYSIS_MODEL is not set and no model_name was provided.")

    def _base_url(self) -> str:
        base_url = self._get_env("ALYSIS_BASE_URL")
        if not base_url:
            raise RuntimeError(
                "ALYSIS_BASE_URL is not set. Point it at an OpenAI-compatible endpoint."
            )
        return base_url

    def _require_clean_build(self) -> str:
        """Whether this campaign refuses an unidentifiable build (default: yes).

        Opt-out rather than opt-in: the failure this guards against is silent,
        and by the time it is noticed the run is already worthless.
        """
        override = str(self._get_env("ALYSIS_REQUIRE_CLEAN_BUILD") or "").strip()
        return override or "1"

    def _install_env(self) -> dict[str, str]:
        env = {
            "PYTHONUNBUFFERED": "1",
            "ALYSIS_BASE_URL": self._base_url(),
            "ALYSIS_CONFIG_DIR": _CONFIG_DIR,
            "ALYSIS_INSTALL_SPEC": self._get_env("ALYSIS_INSTALL_SPEC") or "alysis-code",
            "ALYSIS_MODEL": self._model(),
            "ALYSIS_MODEL_METADATA_POLICY": "warn",
            # setup.sh ends by running `alysis --version`, whose output now
            # carries the commit and build stamp. Forwarding this makes that
            # probe the campaign's first gate: an unidentifiable build is
            # rejected during install, in the setup log, rather than after a
            # few hundred tasks have already produced unattributable scores.
            "ALYSIS_REQUIRE_CLEAN_BUILD": self._require_clean_build(),
            "ALYSIS_SETUP_ARTIFACT_DIR": f"{_ART_DIR}/setup",
            "ALYSIS_SETUP_LOG_DIR": "/logs/agent/setup",
            "ALYSIS_TBENCH_WEB_SEARCH_MODE": self._get_env("ALYSIS_WEB_SEARCH_MODE") or "off",
            "ALYSIS_VERIFY_SANDBOX_MODE": "off",
            "ALYSIS_WHEEL": self._container_wheel_path(),
        }
        return env

    def _container_env(self) -> dict[str, str]:
        env = {
            "ALYSIS_CONFIG_DIR": _CONFIG_DIR,
            "CI": "1",
            "TERM": "dumb",
            "NO_COLOR": "1",
            "ALYSIS_SHELL_SANDBOX_MODE": self._get_env("ALYSIS_SHELL_SANDBOX_MODE") or "off",
            # Refuse to score a build that cannot say which commit it is. An
            # earlier campaign ran against an unpinned "latest main" and three
            # behaviourally different builds all self-reported "0.9.8", so none
            # of those numbers can now be attributed to a source tree. Defaults
            # on for benchmark runs; set ALYSIS_REQUIRE_CLEAN_BUILD=0 on the
            # host to profile a work-in-progress build deliberately.
            "ALYSIS_REQUIRE_CLEAN_BUILD": self._require_clean_build(),
        }
        api_key = self._get_env("ALYSIS_API_KEY")
        if not api_key:
            raise RuntimeError("ALYSIS_API_KEY is not set on the host.")
        env["ALYSIS_API_KEY"] = api_key
        env["ALYSIS_BASE_URL"] = self._base_url()
        ws_key = self._get_env("ALYSIS_WEB_SEARCH_API_KEY")
        if ws_key:
            env["ALYSIS_WEB_SEARCH_API_KEY"] = ws_key
        llm_timeout = self._get_env("ALYSIS_LLM_TIMEOUT_S")
        if llm_timeout:
            env["ALYSIS_LLM_TIMEOUT_S"] = llm_timeout
        # --strict-provenance refuses a run whose thinking setting is not
        # explicit; the host's setting is forwarded unchanged.
        thinking = self._get_env("ALYSIS_LLM_ENABLE_THINKING")
        if thinking:
            env["ALYSIS_LLM_ENABLE_THINKING"] = thinking
        # The process-level web kill switch. The config docstring names this
        # env var as the way to guarantee no web tool is registered; without it
        # on the allowlist a host that set it got the config default instead,
        # and a campaign that believed web was off ran with it on.
        web_tools = self._get_env("ALYSIS_WEB_TOOLS")
        if web_tools:
            env["ALYSIS_WEB_TOOLS"] = web_tools
        if self._deadline_record.get("status") == "ok":
            env["ALYSIS_RUN_BUDGET_SECONDS"] = format(self._deadline_record["seconds"], ".6f")
        for budget_var in (
            "ALYSIS_BUDGET_GRACE_SECONDS",
            "ALYSIS_BUDGET_CHECKPOINT_FRACTION",
        ):
            budget_value = self._get_env(budget_var)
            if budget_value:
                env[budget_var] = budget_value
        return env

    def _install_command(self) -> str:
        return (
            f"mkdir -p {shlex.quote(_WHEEL_DIR)} {shlex.quote(_SETUP_DIR)} "
            f"{shlex.quote(_ART_DIR)}/setup /logs/agent/setup "
            f"&& chmod 777 {shlex.quote(_WHEEL_DIR)} "
            f"&& chmod +x {shlex.quote(_SETUP_SCRIPT)} "
            f"&& {shlex.quote(_SETUP_SCRIPT)}"
        )

    async def install(self, environment: BaseEnvironment) -> None:
        # First, before a directory is made or a byte is uploaded: a campaign
        # whose wheel and adapter disagree must not get as far as building a
        # container, and the record of why must survive the refusal.
        self._write_provenance_artifact()
        self._enforce_provenance()
        await self.exec_as_root(  # type: ignore[attr-defined]
            environment,
            command=(
                f"mkdir -p {shlex.quote(_WHEEL_DIR)} {shlex.quote(_SETUP_DIR)} "
                f"{shlex.quote(_ART_DIR)}/setup /logs/agent/setup "
                f"&& chmod 777 {shlex.quote(_WHEEL_DIR)}"
            ),
        )
        await environment.upload_file(self._host_setup_script_path(), _SETUP_SCRIPT)
        await environment.upload_file(self._host_wheel_path(), self._container_wheel_path())
        await self.exec_as_root(  # type: ignore[attr-defined]
            environment,
            command=self._install_command(),
            env=self._install_env(),
            timeout_sec=_SETUP_TIMEOUT_SEC,
        )

    def _config_set_cmds(self) -> list[str]:
        cmds: list[str] = []
        base_url = self._get_env("ALYSIS_BASE_URL")
        if base_url:
            cmds.append(f"alysis config set base_url {shlex.quote(base_url)}")
        model = self._model()
        if model:
            cmds.append(f"alysis config set model {shlex.quote(model)}")
        ws = {
            "web_search_mode": "ALYSIS_WEB_SEARCH_MODE",
            "web_search_adapter": "ALYSIS_WEB_SEARCH_ADAPTER",
            "web_search_base_url": "ALYSIS_WEB_SEARCH_BASE_URL",
            "web_search_model": "ALYSIS_WEB_SEARCH_MODEL",
            "web_search_timeout_s": "ALYSIS_WEB_SEARCH_TIMEOUT_S",
        }
        for cfg_key, env_name in ws.items():
            val = self._get_env(env_name)
            if val:
                cmds.append(f"alysis config set {cfg_key} {shlex.quote(val)}")
        steps = self._get_env("ALYSIS_MAX_STEPS")
        if steps:
            for cfg_key in ("max_steps", "task_max_steps", "subagent_max_steps"):
                cmds.append(f"alysis config set {cfg_key} {shlex.quote(steps)}")
        for cfg_key, val in self._config_set_passthrough().items():
            cmds.append(f"alysis config set {cfg_key} {shlex.quote(val)}")
        cmds.append(f"alysis config set session_log_dir {shlex.quote(_SESSION_DIR)}")
        cmds.append(f"alysis config set crash_diagnostic_log_path {shlex.quote(_CRASH_LOG)}")
        return cmds

    @with_prompt_template
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        self._host_started = time.monotonic()
        launch_id = uuid.uuid4().hex
        outcome_path = f"{_ART_DIR}/host-outcome-{launch_id}.json"
        outcome_marker = f"ALYSIS_TASK_OUTCOME_{launch_id}="
        supplied = (context.metadata or {}).get("managed_host_deadline", {})
        self._host_metadata = dict(supplied) if isinstance(supplied, dict) else {}
        try:
            self._deadline_resolution()
            env = self._container_env()
            model = self._model()
            base_url = env["ALYSIS_BASE_URL"]
            for setup_command in (
                f"mkdir -p {shlex.quote(_SESSION_DIR)} /logs/agent 2>/dev/null || true",
                " && ".join(self._config_set_cmds()),
            ):
                remaining = self._deadline_resolution()["host_remaining_timeout_seconds"]
                await asyncio.wait_for(
                    self.exec_as_agent(  # type: ignore[attr-defined]
                        environment,
                        command=setup_command,
                        env=env,
                        timeout_sec=remaining,
                    ),
                    timeout=remaining,
                )
            deadline_seconds = self._deadline_resolution()["seconds"]
            env = self._container_env()
            env["ALYSIS_TASK_OUTCOME_PATH"] = outcome_path
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                self._deadline_record["status"] = "deadline_exceeded"
            self._write_provenance_artifact()
            context.metadata = {
                **(context.metadata or {}),
                **self._run_manifest_fields(),
                "alysis_task_outcome": None,
                "alysis_task_outcome_status": self._deadline_record.get("status", "unavailable"),
            }
            raise

        profile = (self._get_env("ALYSIS_RUN_PROFILE") or "auto").strip().lower()
        parts = [
            "alysis",
            "run",
            "--path",
            ".",
            "--allow-broad-workspace",
            "--yes",
            # Streaming chosen on the command line, which --strict-provenance
            # requires. Without a flag `alysis run` already streams (it never
            # reads the `stream false` that setup.sh writes); the flag makes
            # that a recorded choice. The token-plan endpoint closes a
            # non-streaming request at 900 s.
            "--stream",
            "--strict-provenance",
            "--model",
            shlex.quote(model),
            "--base-url",
            shlex.quote(base_url),
            "--api-key-env",
            "ALYSIS_API_KEY",
        ]
        if profile == "benchmark":
            parts.append("--benchmark")
        else:
            parts += ["--mode", shlex.quote(profile)]

        steps = self._get_env("ALYSIS_MAX_STEPS")
        if steps:
            parts += ["--max-steps", shlex.quote(steps)]
        extra = self._get_env("ALYSIS_EXTRA_ARGS")
        if extra:
            extra_parts = shlex.split(extra)
            reserved = {"--deadline-seconds", "--no-deadline", "--require-deadline", "--"}
            if any(part.split("=", 1)[0] in reserved for part in extra_parts):
                raise ValueError("ALYSIS_EXTRA_ARGS cannot override the managed-host deadline")
            provenance_flags = {"--stream", "--no-stream", "--strict-provenance"}
            if any(part.split("=", 1)[0] in provenance_flags for part in extra_parts):
                raise ValueError("ALYSIS_EXTRA_ARGS cannot override streaming or provenance")
            parts.extend(shlex.quote(part) for part in extra_parts)
        parts += ["--deadline-seconds", format(deadline_seconds, ".6f"), "--require-deadline"]
        parts += ["--", shlex.quote(instruction)]

        cmd = compose_run_command(
            (
                "mkdir -p /logs/agent 2>/dev/null; "
                # The wrapper writes the agent's real exit status to a file
                # inside the subshell and re-raises it after tee, so the code
                # the harness sees no longer depends on its shell enabling
                # pipefail. Trial 3's codes arrived intact only because Harbor
                # 0.21.0 happened to execute this pipeline in a shell that
                # preserved them; under plain `sh -c` the same command
                # provably reports tee's 0 instead (see
                # tests/test_session_mirror.py).
                + agent_pipeline_command(
                    " ".join(parts) + " </dev/null",
                    tee_targets=(f"{_ART_DIR}/alysis.txt", "/logs/agent/alysis.txt"),
                )
            ),
            # Linked only after the agent exits: the harvester reads
            # <workspace>/.alysis, but a directory that appears there *during*
            # the run can be swept up by a task's own `git add -A` or trip a
            # tree-state verifier. See session_mirror for the full reasoning.
            mirror_clause=(
                (
                    session_mirror_clause(_SESSION_DIR) + "; "
                    if mirror_enabled(self._get_env(MIRROR_ENV_VAR))
                    else ""
                )
                + outcome_emit_command(path=outcome_path, marker=outcome_marker)
            ),
        )
        process_result = None
        try:
            self._write_provenance_artifact()
            # Command composition and provenance persistence also consume the
            # original host window. Refresh after both before dispatching work.
            launch_deadline = self._deadline_resolution()
            remaining = launch_deadline["host_remaining_timeout_seconds"]
            env[MANAGED_HOST_DEADLINE_UNIX_ENV] = format(
                managed_host_deadline_anchor_unix_seconds(
                    launch_deadline["seconds"],
                    existing_anchor=self._get_env(MANAGED_HOST_DEADLINE_UNIX_ENV),
                    now_unix_seconds=time.time(),
                ),
                ".6f",
            )
            process_result = await asyncio.wait_for(
                self.exec_as_agent(  # type: ignore[attr-defined]
                    environment,
                    command=cmd,
                    env=env,
                    timeout_sec=remaining,
                ),
                timeout=remaining,
            )
        except Exception as exc:
            exit_code = extract_process_exit_code(exc)
            context.metadata = {
                **(context.metadata or {}),
                **run_outcome_metadata(
                    exit_code if exit_code is not None else AGENT_FAILURE_EXIT_CODE
                ),
                **self._run_manifest_fields(),
                **outcome_metadata(exc, marker=outcome_marker),
            }
            raise
        else:
            context.metadata = {
                **(context.metadata or {}),
                **run_outcome_metadata(SUCCESS_EXIT_CODE),
                **self._run_manifest_fields(),
                **outcome_metadata(process_result, marker=outcome_marker),
            }

    def populate_context_post_run(self, context: AgentContext) -> None:
        context.metadata = {
            **(getattr(context, "metadata", None) or {}),
            **self._run_manifest_fields(),
        }
