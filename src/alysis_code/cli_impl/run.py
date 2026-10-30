"""Canonical one-shot execution, independent of interactive chat rendering.

Typer argument handling, subscription and filesystem admission remain in the
root command. This module owns the same configuration, workspace and execution
path used by both direct CLI and desktop-managed runs.
"""

from __future__ import annotations

import os
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import typer
from click import get_current_context
from click.core import ParameterSource

from ..branding import env_get
from ..config import ConfigError, clone_cfg, load_config
from ..error_text import sanitize_error_text_for_output
from ..failure_category import exit_code_for_failure
from ..personas import (
    get_persona,
    is_persona_name,
    persona_modes_enabled,
    resolve_persona_exec_mode,
)
from ..profiles import apply_runtime_base_url_override
from ..run_outcome import INFRASTRUCTURE_FAILURE_EXIT_CODE
from ..runtime_kind import RuntimeKind
from ..surface.console import safe_plain_error
from ..workspace_binding import WorkspaceAction, WorkspaceBindingError
from .commands import _patchable as _cli_patchable
from .commands._shared import (
    Mode,
    _console,
)
from .execution_admission import _apply_temperature_override, _resolve_api_key_override

_RUN_DEPENDENCIES: ContextVar[dict[str, Any] | None] = ContextVar("run_dependencies", default=None)


def _patchable(name: str, default: Any) -> Any:
    dependencies = _RUN_DEPENDENCIES.get()
    if dependencies is not None and name in dependencies:
        return dependencies[name]
    return _cli_patchable(name, default)


def run_impl(
    cli_mod: Any,
    *args: Any,
    dependency_overrides: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    # Read actual embedding overrides without materializing the interactive
    # compatibility facade. Keep invocation state isolated across nested calls.
    dependencies = {**vars(cli_mod), **(dependency_overrides or {})}
    token = _RUN_DEPENDENCIES.set(dependencies)
    try:
        return run(*args, **kwargs)
    finally:
        _RUN_DEPENDENCIES.reset(token)


def _is_non_interactive_terminal() -> bool:
    return not (sys.stdin.isatty() and sys.stdout.isatty())


def _resolve_startup_workspace_binding(**kwargs: Any) -> Any:
    if kwargs["interactive"]:
        # Interactive selectors keep their existing TUI and CLI override paths.
        from .. import cli
        from .commands import _materialize_cli_globals

        _materialize_cli_globals(cli)
        from .commands.chat_terminal import _resolve_startup_workspace_binding as resolve
    else:
        from ..workspace_binding_ui import resolve_startup_workspace_binding as resolve
    return resolve(**kwargs)


def _make_rich_surface(**kwargs: Any) -> Any:
    from ..surface.rich_surface import RichSurface

    return RichSurface(**kwargs)


def run_agent(*args: Any, **kwargs: Any) -> Any:
    from ..cli_run_deadline import finish_expired_managed_launch

    if not args:
        expired = finish_expired_managed_launch(**kwargs)
        if expired is not None:
            return expired
    from ..agent_loop import run_agent as execute

    return execute(*args, **kwargs)


_RAW_BENCHMARK_PROFILE_NAMES: set[str] = {
    "benchmark",
    "bench",
    "raw-agent",
    "raw_agent",
    "raw-benchmark",
    "raw_benchmark",
    "raw-agent-benchmark",
    "raw_agent_benchmark",
}
_RAW_BENCHMARK_MIN_MAX_STEPS = 80


def _path_binding_source(path_source: Any, path: Any) -> str:
    if path_source is not None and path_source is not ParameterSource.DEFAULT:
        return "explicit_path"
    if path not in (None, ".", Path(".")):
        return "explicit_path"
    return "cwd"


def _raw_benchmark_profile_requested(*, benchmark: bool) -> bool:
    if benchmark:
        return True
    profile = env_get("ALYSIS_RUN_PROFILE", "") or ""
    normalized = profile.strip().lower()
    return normalized in _RAW_BENCHMARK_PROFILE_NAMES


def _apply_raw_benchmark_profile(cfg: Any) -> None:
    cfg.default_mode = "auto"
    cfg.routing_mode = "code_only"
    cfg.subagents_enabled = False
    cfg.skills_enabled = False
    cfg.skills_auto_invoke = False
    cfg.custom_tools_enabled = False
    cfg.web_search_mode = "off"
    cfg.max_steps = max(int(getattr(cfg, "max_steps", 0) or 0), _RAW_BENCHMARK_MIN_MAX_STEPS)


def _load_run_seed_messages(
    *,
    session_id: str | None,
    session_dir: Path | None,
    managed_session_persistence: Any = None,
) -> list[dict[str, Any]] | None:
    """Load prior turns for a standalone named run without duplicating App-owned context."""

    if not session_id:
        return None
    owned_persistence = None
    try:
        from ..managed_session import (
            normalize_managed_session_id,
            prepare_managed_session_persistence,
        )
        from .commands.chat_resume_helpers import _load_chat_resume_messages

        normalized = normalize_managed_session_id(session_id)
        persistence = managed_session_persistence
        if persistence is None:
            if session_dir is not None:
                sessions_dir = Path(session_dir)
            else:
                from ..config import default_sessions_dir

                sessions_dir = default_sessions_dir()
            owned_persistence = prepare_managed_session_persistence(
                sessions_dir=sessions_dir,
                session_id=normalized,
                append=False,
            )
            owned_persistence.seal()
            persistence = owned_persistence
        if persistence.session_id != normalized:
            return None
        persistence.require_sealed()
        loaded = _load_chat_resume_messages(
            None,
            event_records=persistence.events_snapshot(),
        )
        if owned_persistence is not None:
            owned_persistence.close()
        return loaded or None
    except Exception:  # noqa: BLE001 - a corrupt prior log must not block a fresh turn.
        try:
            if owned_persistence is not None:
                owned_persistence.close()
        except Exception:  # noqa: BLE001
            pass
        if managed_session_persistence is not None:
            # An explicit App-managed boundary is fail-closed; only legacy
            # implicit/default history loading retains best-effort semantics.
            raise
        return None


def run(
    instruction: str = typer.Argument(..., help="What you want the agent to do."),
    path: Path = typer.Option(Path("."), "--path", help="Working directory/root."),
    create_path: bool = typer.Option(
        False,
        "--create-path",
        help="Create --path if it does not exist before binding the workspace.",
    ),
    allow_broad_workspace: bool = typer.Option(
        False,
        "--allow-broad-workspace",
        help="Allow guarded broad workspaces in non-interactive startup flows.",
    ),
    image: list[Path] | None = typer.Option(
        None,
        "--image",
        help="Attach image path(s). Repeat --image for multiple files.",
    ),
    mode: Mode | None = typer.Option(None, "--mode", help="Mode override."),
    persona: str | None = typer.Option(
        None,
        "--persona",
        help=(
            "Persona for this one-shot run: code, architect, ask, debug, or a "
            "custom persona from .alysis_personas."
        ),
    ),
    model: str | None = typer.Option(None, "--model", help="Model override."),
    base_url: str | None = typer.Option(None, "--base-url", help="Base URL override."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Enable streamed assistant output.",
    ),
    max_steps: int | None = typer.Option(
        None,
        "--max-steps",
        help="Optional safety limit on agent iterations.",
    ),
    subagents: bool | None = typer.Option(
        None,
        "--subagents/--no-subagents",
        help="Enable or disable subagent delegation for this session.",
    ),
    no_log: bool = typer.Option(False, "--no-log", help="Disable JSONL session logging."),
    verify_cmd: list[str] | None = typer.Option(
        None,
        "--verify-cmd",
        help="Override verification command for this run (repeatable).",
    ),
    api_key_env: str | None = typer.Option(
        None,
        "--api-key-env",
        help=(
            "Read API key from this environment variable (overrides ALYSIS_API_KEY/OPENAI_API_KEY)."
        ),
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help="Prompt for API key (hidden input). Key is kept in memory for this run only.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help=(
            "UNSAFE: Provide API key via CLI argument (may leak via shell history / process list). "
            "Prefer --api-key-stdin or --api-key-env."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="In auto mode, skip confirmations for sensitive commands (hard blocks still apply).",
    ),
    benchmark: bool = typer.Option(
        False,
        "--benchmark",
        help=(
            "Use the raw benchmark/autonomy run profile: auto mode, code-only routing, "
            "longer fixed step budget, and no subagents/skills/custom tools/web by default."
        ),
    ),
    deadline_seconds: float | None = typer.Option(
        None,
        "--deadline-seconds",
        help=(
            "Stop this one-shot run after the given invocation-wide wall-clock seconds. "
            "Defaults to 3600 (60 min); use --no-deadline to run unbounded."
        ),
    ),
    no_deadline: bool = typer.Option(
        False,
        "--no-deadline",
        help="Run without any wall-clock budget, overriding the default one-shot deadline.",
    ),
    require_deadline: bool = typer.Option(
        False,
        "--require-deadline",
        help=(
            "Require a finite one-shot run deadline from CLI, environment, or config. "
            "Intended for managed hosts."
        ),
    ),
    diagnostic_log: Path | None = typer.Option(
        None,
        "--diagnostic-log",
        help="Append minimal crash-safe diagnostic events to this JSONL path.",
    ),
    session_id: str | None = typer.Option(
        None,
        "--session",
        help="Resume (or start) a named session id. Used by the desktop chat.",
    ),
    session_dir: Path | None = typer.Option(
        None,
        "--session-dir",
        help="Directory holding session JSONL logs (defaults to the standard sessions dir).",
    ),
    cli_ctx: Any = None,
    strict_provenance: bool = False,
    managed_session_persistence: Any = None,
) -> None:
    console = _patchable("_console", _console)()
    cfg = _patchable("load_config", load_config)()
    current_ctx = cli_ctx if cli_ctx is not None else get_current_context(silent=True)
    non_interactive = _patchable("_is_non_interactive_terminal", _is_non_interactive_terminal)()
    path_source = current_ctx.get_parameter_source("path") if current_ctx is not None else None
    max_steps_source = (
        current_ctx.get_parameter_source("max_steps") if current_ctx is not None else None
    )
    max_steps_provided = max_steps is not None
    if current_ctx is not None:
        max_steps_provided = (
            max_steps is not None
            and max_steps_source is not None
            and max_steps_source is not ParameterSource.DEFAULT
        )

    effective = _patchable("clone_cfg", clone_cfg)(cfg)
    raw_benchmark_profile = _raw_benchmark_profile_requested(benchmark=benchmark)
    if raw_benchmark_profile:
        _apply_raw_benchmark_profile(effective)
    if base_url is not None:
        apply_runtime_base_url_override(effective, base_url)
    if model is not None:
        effective.model = model
    if temperature is not None:
        _patchable("_apply_temperature_override", _apply_temperature_override)(
            effective, temperature
        )
    if stream is not None:
        effective.stream = stream
    else:
        # Interactive chat is more readable with incremental output by default.
        effective.stream = True
    if max_steps is not None:
        effective.max_steps = max_steps
    if subagents is not None:
        effective.subagents_enabled = subagents
    if strict_provenance is True:
        problems = _strict_provenance_problems(stream=stream, cfg=effective)
        if problems:
            console.print(
                "[red]Config error:[/red] --strict-provenance refuses to start: "
                + "; ".join(problems)
            )
            raise typer.Exit(code=2)

    effective_mode = (mode.value if mode else effective.default_mode) or "review"
    effective_yes = bool(yes or raw_benchmark_profile)
    fixed_step_override = (
        effective.max_steps if (max_steps_provided or raw_benchmark_profile) else None
    )
    delegated_execution = effective.execution.backend == "delegated"
    if delegated_execution and mode is None and str(effective_mode).strip().lower() == "fullaccess":
        effective_mode = "review"
        console.print(
            "[yellow]Delegated runtimes do not inherit the native fullaccess default; "
            "using review (read-only). Pass --mode auto to allow workspace writes.[/yellow]"
        )
    binding_source = _path_binding_source(path_source, path)

    try:
        if session_id is not None:
            from ..managed_session import normalize_managed_session_id

            session_id = normalize_managed_session_id(session_id)
            if managed_session_persistence is None:
                raise ConfigError("Managed session filesystem boundary is unavailable")
            if managed_session_persistence.session_id != session_id:
                raise ConfigError("Managed session filesystem boundary does not match --session")
            managed_session_persistence.require_sealed()
            session_dir = managed_session_persistence.sessions_dir
        elif session_dir is not None or managed_session_persistence is not None:
            raise ConfigError("--session-dir requires --session")
        if delegated_execution and session_id is not None:
            raise ConfigError(
                "--session/--session-dir managed persistence is not supported with "
                "delegated execution"
            )
        if not delegated_execution and not effective.model:
            raise ConfigError("Model is not set. Run: alysis config set model <MODEL>")
        api_key_override = None
        if delegated_execution:
            from ..agent_runtimes.host import (
                prepare_delegated_runtime,
                run_delegated_once,
                validate_delegated_cli_options,
            )

            validate_delegated_cli_options(
                base_url=base_url,
                temperature=temperature,
                max_steps=max_steps,
                subagents=subagents,
                verify_cmd=verify_cmd,
                api_key_env=api_key_env,
                api_key_stdin=api_key_stdin,
                api_key=api_key,
                stream=stream,
                yes=yes,
                benchmark=raw_benchmark_profile,
                diagnostic_log=diagnostic_log,
            )
        else:
            api_key_override = _patchable("_resolve_api_key_override", _resolve_api_key_override)(
                api_key=api_key,
                api_key_env=api_key_env,
                api_key_stdin=api_key_stdin,
            )
        workspace_binding = _patchable(
            "_resolve_startup_workspace_binding", _resolve_startup_workspace_binding
        )(
            requested_path=path,
            console=console,
            interactive=not non_interactive,
            create_if_missing=create_path,
            allow_broad_workspace=allow_broad_workspace,
            source=binding_source,
            action=WorkspaceAction.CHAT,
        )
        run_persona_overlays: list[str] | None = None
        run_persona_user_overlays: list[str] | None = None
        run_persona_globs: list[str] | None = None
        if persona is not None:
            from ..config import resolve_role_temperature
            from ..model_router import resolve_model_for_role
            from ..personas import (
                load_custom_personas,
            )
            from ..personas import (
                persona_overlay_messages as _persona_overlays_fn,
            )
            from ..personas import (
                persona_overlay_user_messages as _persona_user_overlays_fn,
            )
            from ..personas import (
                resolve_persona_model_role as _resolve_persona_role,
            )

            normalized_run_persona = str(persona).strip().lower()
            if delegated_execution:
                console.print("[red]--persona is not supported with delegated execution.[/red]")
                raise typer.Exit(code=2)
            if not persona_modes_enabled(effective):
                console.print("[red]Persona modes are disabled.[/red]")
                raise typer.Exit(code=2)
            run_registry, run_persona_warnings = load_custom_personas(
                workspace_binding.workspace_context.workspace_root
            )
            for warning in run_persona_warnings:
                console.print(f"[yellow]Custom persona:[/yellow] {warning}")
            if not is_persona_name(normalized_run_persona, run_registry):
                console.print(f"[red]Unknown persona:[/red] {normalized_run_persona}")
                raise typer.Exit(code=2)
            run_definition = get_persona(normalized_run_persona, run_registry)
            # Same clamp as interactive switching: the persona may lower the
            # requested mode, never raise it.
            effective_mode = resolve_persona_exec_mode(run_definition, effective_mode)
            if run_definition.allow_write_globs:
                run_persona_globs = list(run_definition.allow_write_globs)
            run_persona_overlays = (
                _persona_overlays_fn(
                    cfg=effective, persona=normalized_run_persona, registry=run_registry
                )
                or None
            )
            run_persona_user_overlays = (
                _persona_user_overlays_fn(
                    cfg=effective, persona=normalized_run_persona, registry=run_registry
                )
                or None
            )
            if model is None:
                persona_role = _resolve_persona_role(
                    effective, normalized_run_persona, run_registry
                )
                resolved_persona_model = str(
                    resolve_model_for_role(cfg=effective, role=persona_role, plan=None) or ""
                ).strip()
                if resolved_persona_model and resolved_persona_model != effective.model:
                    effective.model = resolved_persona_model
            else:
                persona_role = _resolve_persona_role(
                    effective, normalized_run_persona, run_registry
                )
            # create_session constructs the one-shot main client through the
            # coding slot. Carry the selected persona role's temperature into
            # that slot so explicit zero values and per-role tuning survive.
            effective.coding_temperature = resolve_role_temperature(
                effective,
                role=persona_role,
            )
            effective.default_persona = normalized_run_persona
        if delegated_execution:
            from ..config import resolve_run_deadline
            from ..execution_deadline import DEFAULT_RUN_DEADLINE_SECONDS

            delegated_default_deadline = (
                None if (require_deadline or no_deadline) else DEFAULT_RUN_DEADLINE_SECONDS
            )
            resolved_deadline = resolve_run_deadline(
                effective,
                cli_deadline_seconds=deadline_seconds,
                cli_no_deadline=no_deadline,
                default_seconds=delegated_default_deadline,
            )
            delegated_deadline = resolved_deadline.seconds
            if require_deadline and delegated_deadline is None:
                raise ConfigError(
                    "Managed-host run requires a finite run deadline. Pass --deadline-seconds, "
                    "set ALYSIS_RUN_DEADLINE_SECONDS, or configure run_deadline_seconds."
                )
            prepare_delegated_runtime(
                effective,
                model=model,
                deadline_seconds=delegated_deadline,
            )
            code = run_delegated_once(
                cfg=effective,
                cwd=workspace_binding.workspace_context.workspace_root,
                instruction=instruction,
                mode=effective_mode,
                image_paths=tuple(path.resolve() for path in (image or ())),
                no_log=no_log,
                console=console,
            )
        else:
            app_managed_context = instruction.lstrip().startswith(
                ("[Alysis Desktop context", "[Alysis Desktop context")
            )
            seed_messages = (
                None
                if app_managed_context
                else _load_run_seed_messages(
                    session_id=session_id,
                    session_dir=session_dir,
                    managed_session_persistence=managed_session_persistence,
                )
            )
            code = _patchable("run_agent", run_agent)(
                cfg=effective,
                root=workspace_binding.workspace_context.workspace_root,
                instruction=instruction,
                image_paths=[os.fspath(p) for p in (image or [])],
                mode=effective_mode,
                persona_allow_write_globs=run_persona_globs,
                ephemeral_system_messages=run_persona_overlays,
                ephemeral_user_messages=run_persona_user_overlays,
                runtime_kind=(
                    RuntimeKind.INTERACTIVE_CHAT if app_managed_context else RuntimeKind.ONE_SHOT
                ),
                yes=effective_yes,
                max_steps=effective.max_steps,
                no_log=no_log,
                api_key_override=api_key_override,
                console=console,
                surface=_patchable("_make_rich_surface", _make_rich_surface)(console=console),
                non_interactive=non_interactive,
                usage_role="run",
                subagents_enabled=effective.subagents_enabled,
                # Desktop Chat owns a persistent conversation and may contain
                # ordinary questions. Keep strict one-shot apply/verify gates
                # for canonical ``alysis run`` while allowing the observed-
                # activity interactive gate to govern app-managed turns.
                one_shot_execution=not app_managed_context,
                enable_chat_turn_step_budget=True,
                chat_turn_fixed_override=fixed_step_override,
                enable_tool_output_offload=(True if raw_benchmark_profile else None),
                compaction_profile="execution",
                verify_cmd=verify_cmd,
                workspace_binding=workspace_binding,
                run_deadline_seconds=deadline_seconds,
                no_run_deadline=no_deadline,
                require_run_deadline=require_deadline,
                crash_diagnostic_log_path=diagnostic_log,
                session_log_dir_override=session_dir,
                session_id_override=session_id,
                seed_messages=seed_messages,
                managed_session_persistence=managed_session_persistence,
            )
    except ConfigError as e:
        console.print(f"[red]Config error:[/red] {e}")
        raise typer.Exit(code=2) from e
    except WorkspaceBindingError as e:
        console.print(f"[red]Workspace error:[/red] {e}")
        raise typer.Exit(code=1) from e
    except Exception as e:  # noqa: BLE001
        # Prefer friendly Alysis Code MiMo trial copy (trial_expired, rate-limit, ...)
        # over a raw ``LLM error 402: {...}`` dump; any other error renders as-is.
        message = sanitize_error_text_for_output(e)
        try:
            from ..llm.openai_compat import alysis_trial_error_message

            message = sanitize_error_text_for_output(alysis_trial_error_message(e) or message)
        except Exception:  # noqa: BLE001
            pass
        exit_code = exit_code_for_failure(e)
        label = (
            _infrastructure_failure_label(e)
            if exit_code == INFRASTRUCTURE_FAILURE_EXIT_CODE
            else "Error"
        )
        try:
            console.print(f"[red]{label}:[/red] {message}")
        except Exception as render_exc:  # noqa: BLE001 - CLI error rendering must not double-crash.
            safe_plain_error(
                stream=getattr(console, "file", None),
                error_type=type(render_exc).__name__,
                message=sanitize_error_text_for_output(e),
            )
        raise typer.Exit(code=exit_code) from e

    raise typer.Exit(code=code)


def _strict_provenance_problems(*, stream: bool | None, cfg: Any) -> list[str]:
    """What a scored run would otherwise leave to a default, one line each.

    Streaming must be chosen on the command line, because ``run`` otherwise
    streams by default without recording that anyone chose it. Thinking must
    resolve to true or false, because an unset value leaves the provider's own
    default in force while the session records ``None``.
    """
    from ..config import resolve_llm_enable_thinking

    problems: list[str] = []
    if stream is None:
        problems.append("streaming is not explicit (pass --stream or --no-stream)")
    if resolve_llm_enable_thinking(cfg) is None:
        problems.append(
            "thinking is not explicit (set ALYSIS_LLM_ENABLE_THINKING=true|false "
            "or llm_enable_thinking)"
        )
    return problems


def _infrastructure_failure_label(error: BaseException) -> str:
    """Console label for an infrastructure failure, with the real counts.

    States attempts and retries as numbers whenever the provider retry loop
    recorded them, so a failure that was never retried cannot be reported as
    having been retried.
    """
    from ..llm.provider_limits import provider_attempt_counts

    counts = provider_attempt_counts(error)
    if counts is None:
        return "Infrastructure error"
    attempts, retries = counts
    attempt_word = "attempt" if attempts == 1 else "attempts"
    retry_word = "retry" if retries == 1 else "retries"
    return f"Infrastructure error after {attempts} {attempt_word} ({retries} {retry_word})"
