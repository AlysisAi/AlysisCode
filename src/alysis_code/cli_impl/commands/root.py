from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from ... import __version__
from ...auth_diagnostics import auth_doctor_payload
from ...build_identity import decide_clean_build, version_line
from ...config import (
    AppConfig,
    ConfigError,
    config_path,
    default_sessions_dir,
    load_config,
    resolve_model_access_api_key,
    resolve_web_search_policy,
)
from ...provider_diagnostics import (
    build_provider_diagnostics,
    probe_reasoning_suppression_live,
    validate_active_provider_live,
    validate_web_search_live,
)
from ...provider_telemetry import (
    diagnostic_bundle_payload,
    last_provider_call_summary,
    last_web_search_summary,
)
from ...sandbox_doctor import diagnose_sandbox
from ...skills import resolve_skills_enabled
from ...step_budget import normalize_step_budget_policy
from ...tools.availability import get_tool_availability
from ...tools.registry import iter_builtin_tool_metadata
from ...tools.web_search import resolve_web_search_runtime_status
from ..assets_cli import assets_app as forge_assets_app
from . import _patchable
from ._shared import (
    Mode,
    _console,
    _Table,
)
from .agents import agents_app
from .auth import JSON_OPTION_HELP as _JSON_OPTION_HELP
from .auth import _status_payload as _auth_status_payload
from .auth import auth_app
from .config import config_app
from .conventions import conventions_app
from .extensions import ext_app
from .forge import forge_app
from .hooks import hooks_app
from .ide_bridge import ide_bridge_app
from .mcp import mcp_app, mcp_auth_app, mcp_prompts_app
from .profile import profile_app
from .report import report_app
from .sandbox import sandbox_app
from .server import server_app
from .sessions import sessions_app
from .skills import skill_app
from .tools import tool_app
from .update import (
    _BACKGROUND_UPDATE_SUBCOMMANDS,
    _cached_update_status_summary,
    _start_background_update_check,
    maybe_prompt_update_at_startup,
    update_app,
)

if TYPE_CHECKING:
    from rich.table import Table


def _cli_module() -> Any:
    module = sys.modules.get("alysis_code.cli")
    if module is not None:
        return module
    from ... import cli

    return cli


app = typer.Typer(add_completion=False, help="Local CLI coding agent (multi-provider).")
app.add_typer(auth_app, name="auth")
app.add_typer(config_app, name="config")
app.add_typer(profile_app, name="profile")
app.add_typer(update_app, name="update")
app.add_typer(sessions_app, name="sessions")
app.add_typer(forge_app, name="forge")
forge_app.add_typer(forge_assets_app, name="assets")
app.add_typer(server_app, name="server")
app.add_typer(ext_app, name="ext")
app.add_typer(report_app, name="report")
app.add_typer(mcp_app, name="mcp")
app.add_typer(skill_app, name="skill")
app.add_typer(agents_app, name="agents")
app.add_typer(conventions_app, name="conventions")
app.add_typer(tool_app, name="tool")
app.add_typer(hooks_app, name="hooks")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(ide_bridge_app, name="ide-bridge")
mcp_app.add_typer(mcp_prompts_app, name="prompts")
mcp_app.add_typer(mcp_auth_app, name="auth")


#: A build that cannot identify itself is a configuration problem, not a task
#: failure, so it gets its own code rather than the generic 1.
UNIDENTIFIABLE_BUILD_EXIT_CODE = 2


@app.command()
def capabilities() -> None:
    """Emit the credential-free local surface capability contract."""

    from ...capabilities import build_capabilities_payload

    typer.echo(json.dumps(build_capabilities_payload(), sort_keys=True))


def _show_version(value: bool) -> None:
    if not value:
        return
    # Version, commit, build timestamp and dirty flag on one line, version
    # first. Three behaviourally different builds once all reported "0.9.8",
    # which made every score they produced unattributable; the bare version is
    # kept as the leading token because the VS Code extension, the managed-CLI
    # smoke test and the release distribution validator all read this output.
    typer.echo(version_line(__version__))
    raise typer.Exit()


def _enforce_clean_build(flag: bool) -> None:
    """Refuse to start when the build cannot prove what it is.

    Benchmark harnesses set ``ALYSIS_REQUIRE_CLEAN_BUILD=1`` so a campaign
    cannot silently run against an unpinned or locally-modified build. One
    earlier campaign did exactly that -- it ran against "latest main" -- and
    none of its results can now be tied to a source tree.

    Deliberately after ``--version``, which is eager: diagnosing a refusal
    starts by asking the binary what it thinks it is, and that has to keep
    working.
    """
    decision = decide_clean_build(flag=flag)
    if decision.allowed:
        return
    typer.echo(f"alysis: {decision.message}", err=True)
    raise typer.Exit(code=UNIDENTIFIABLE_BUILD_EXIT_CODE)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        callback=_show_version,
        is_eager=True,
        help="Show the installed Alysis Code version, commit and build stamp, then exit.",
    ),
    require_clean_build: bool = typer.Option(
        False,
        "--require-clean-build",
        help=(
            "Refuse to run unless this build recorded a commit and a clean tree at "
            "build time. Also enabled by ALYSIS_REQUIRE_CLEAN_BUILD=1."
        ),
    ),
) -> None:
    # Before anything else, and before the subcommand dispatch below, so that
    # `alysis run ...` under a benchmark harness is refused just as the bare
    # invocation is.
    _enforce_clean_build(require_clean_build)
    cli = _cli_module()
    if ctx.invoked_subcommand is not None:
        if ctx.invoked_subcommand in _BACKGROUND_UPDATE_SUBCOMMANDS:
            _start_background_update_check()
        return
    console = cli._console()
    if cli._is_interactive_terminal():
        _start_background_update_check()
    if cli._is_non_interactive_terminal():
        console.print(cli._home_panel())
        return
    maybe_prompt_update_at_startup()
    if not cli._home_prompt_enabled():
        if not cli._maybe_run_first_run_setup_wizard():
            return
        cli._maybe_run_startup_config_menu()
        cli._run_default_chat_action()
        return
    console.print(cli._home_panel())
    try:
        action = (
            typer.prompt(
                "Action [1=chat|2=run|3=setup|4=doctor|5=plan|6=quit]",
                default="1",
            )
            .strip()
            .lower()
        )
    except (EOFError, KeyboardInterrupt):
        console.print("")
        return
    action = cli._HOME_ACTION_ALIASES.get(action, action)

    if action in {"quit", "q", "exit"}:
        return
    if action in {"chat", "c"}:
        cli._maybe_run_startup_config_menu()
        cli._run_default_chat_action()
        return
    if action in {"run", "r"}:
        instruction = typer.prompt("Instruction").strip()
        if instruction:
            cli._run_default_run_action(instruction)
        return
    if action in {"setup", "s"}:
        cli.setup()
        return
    if action in {"doctor", "d"}:
        cli.doctor()
        return
    if action in {"plan", "p"}:
        cli.forge_plan(path=Path("."))
        return
    console.print("[yellow]Unknown action.[/yellow] Run `alysis --help`.")


def _require_active_subscription_ready(
    *,
    model: str | None,
    base_url: str | None,
    require_ready: bool = True,
) -> None:
    _cli_module()._require_active_subscription_ready(
        model=model,
        base_url=base_url,
        require_ready=require_ready,
    )


@app.command()
def run(
    ctx: typer.Context = None,
    instruction: str | None = typer.Argument(None, help="What you want the agent to do."),
    instruction_stdin: bool = typer.Option(
        False,
        "--instruction-stdin",
        help="Read the instruction from stdin. Intended for trusted desktop/managed hosts.",
    ),
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
        help=("Persona: code, architect, ask, debug, or a custom persona from .alysis_personas."),
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
    strict_provenance: bool = typer.Option(
        False,
        "--strict-provenance",
        help=(
            "Refuse to start unless streaming (--stream/--no-stream) and thinking "
            "(ALYSIS_LLM_ENABLE_THINKING or llm_enable_thinking) are set explicitly. "
            "Benchmark adapters pass this for scored runs."
        ),
    ),
    session_id: str | None = typer.Option(
        None,
        "--session",
        help=(
            "Resume (or start) a named session id so the run continues that conversation. "
            "Used by the desktop chat for persistent multi-turn sessions."
        ),
    ),
    session_dir: Path | None = typer.Option(
        None,
        "--session-dir",
        help="Directory holding session JSONL logs (defaults to the standard sessions dir).",
    ),
) -> None:
    if instruction_stdin and api_key_stdin:
        typer.echo("Do not combine --instruction-stdin with --api-key-stdin.", err=True)
        raise typer.Exit(2)
    if instruction_stdin:
        if instruction is not None:
            typer.echo("Do not combine INSTRUCTION with --instruction-stdin.", err=True)
            raise typer.Exit(2)
        instruction = sys.stdin.read()
    instruction = str(instruction or "")
    if not instruction.strip():
        typer.echo("Missing argument 'INSTRUCTION': instruction is empty.", err=True)
        raise typer.Exit(1)
    if session_dir is not None and session_id is None:
        typer.echo("Config error: --session-dir requires --session.", err=True)
        raise typer.Exit(2)

    managed_session_persistence = None
    if session_id is not None:
        from ...managed_session import prepare_managed_session_persistence

        try:
            managed_session_persistence = prepare_managed_session_persistence(
                sessions_dir=session_dir if session_dir is not None else default_sessions_dir(),
                session_id=session_id,
                append=not no_log,
            )
            # Finalize the exact retained writer (or read-only capability) now.
            # No later path lookup or fdopen failure may occur after provider
            # readiness begins.
            managed_session_persistence.seal()
        except ConfigError as exc:
            if managed_session_persistence is not None:
                managed_session_persistence.close()
            typer.echo(f"Config error: {exc}", err=True)
            raise typer.Exit(2) from exc
        session_id = managed_session_persistence.session_id
        session_dir = managed_session_persistence.sessions_dir

    try:
        # The managed session filesystem boundary above must be admitted before
        # subscription/provider setup so an invalid App destination cannot
        # begin a model-backed run.
        _require_active_subscription_ready(model=model, base_url=base_url)

        from ..chat import run_impl

        return run_impl(
            _cli_module(),
            instruction,
            path,
            create_path,
            allow_broad_workspace,
            image,
            mode,
            persona,
            model,
            base_url,
            temperature,
            stream,
            max_steps,
            subagents,
            no_log,
            verify_cmd,
            api_key_env,
            api_key_stdin,
            api_key,
            yes,
            benchmark,
            deadline_seconds,
            no_deadline,
            require_deadline,
            diagnostic_log,
            session_id,
            session_dir,
            cli_ctx=ctx,
            strict_provenance=strict_provenance,
            managed_session_persistence=managed_session_persistence,
        )
    finally:
        if managed_session_persistence is not None:
            managed_session_persistence.close()


@app.command()
def chat(
    ctx: typer.Context = None,
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
        help="Queue image path(s) for the next message. Repeat --image for multiple files.",
    ),
    mode: Mode | None = typer.Option(None, "--mode", help="Mode override."),
    persona: str | None = typer.Option(
        None,
        "--persona",
        help=("Persona: code, architect, ask, debug, or a custom persona from .alysis_personas."),
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
        help="Optional safety limit on agent iterations for each user turn.",
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
        help="Override verification command for this chat session (repeatable).",
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
        help="In auto mode, skip confirmations for sensitive commands.",
    ),
    diagnostic_log: Path | None = typer.Option(
        None,
        "--diagnostic-log",
        help="Append minimal crash-safe diagnostic events to this JSONL path.",
    ),
) -> None:
    _require_active_subscription_ready(
        model=model,
        base_url=base_url,
        require_ready=False,
    )

    from ..chat import chat_impl

    return chat_impl(
        _cli_module(),
        path,
        create_path,
        allow_broad_workspace,
        image,
        mode,
        persona,
        model,
        base_url,
        temperature,
        stream,
        max_steps,
        subagents,
        no_log,
        verify_cmd,
        api_key_env,
        api_key_stdin,
        api_key,
        yes,
        diagnostic_log,
        cli_ctx=ctx,
    )


def _doctor_table(cfg: AppConfig) -> Table:
    table = _Table(title="alysis doctor")
    table.add_column("check")
    table.add_column("result")
    api_key = _cli_module()._resolved_api_key_value()

    table.add_row("python", sys.version.split()[0])

    def _cmd_exists(cmd: str) -> bool:
        return shutil.which(cmd) is not None

    table.add_row("git", "ok" if _cmd_exists("git") else "missing")
    table.add_row(
        "rg",
        "ok" if _cmd_exists("rg") else "missing (search fallback will be slower)",
    )
    table.add_row("config_path", os.fspath(config_path()))
    table.add_row(
        "model_set",
        "yes" if bool(cfg.model) else "no (run: alysis config set model <MODEL>)",
    )
    table.add_row(
        "api_key_set",
        (
            f"yes ({_cli_module()._api_key_source_label(api_key.source)})"
            if api_key.key
            else "no (run: alysis config set-api-key)"
        ),
    )
    table.add_row("base_url", cfg.base_url)
    table.add_row("update", _cached_update_status_summary(cfg))
    web_search_status = resolve_web_search_runtime_status(cfg=cfg, api_key=api_key.key)
    web_search_policy = resolve_web_search_policy(cfg)
    web_search_label = (
        "policy-disabled" if web_search_policy == "off" else web_search_status.availability_label
    )
    web_search_setup = (
        "Set `alysis config set web_search_policy auto` to expose search to the model."
        if web_search_policy == "off"
        else web_search_status.setup_hint
    )
    table.add_row("web_search", web_search_label)
    table.add_row("web_search_policy", web_search_policy)
    table.add_row("web_search_provider", web_search_status.provider or "(none)")
    table.add_row("web_search_setup", web_search_setup)
    try:
        sandbox_status = _patchable("diagnose_sandbox", diagnose_sandbox)(
            cfg, include_smoke=False, include_server_image=False
        )
        sandbox_label = (
            "ready" if sandbox_status.ready else "not ready (run: alysis doctor sandbox)"
        )
        table.add_row("sandbox", sandbox_label)
        table.add_row("sandbox_backend", sandbox_status.selected_backend or "(none)")
    except ConfigError as exc:
        table.add_row("sandbox", f"config error ({exc})")
    table.add_row("temperature", str(cfg.temperature))
    table.add_row("coding_temperature", str(cfg.coding_temperature))
    table.add_row("chat_temperature", str(cfg.chat_temperature))
    execution_policy = normalize_step_budget_policy(cfg.step_budget_policy)
    table.add_row("execution_policy", execution_policy)
    if execution_policy == "autonomous":
        table.add_row("step_limit", "unlimited")
    else:
        table.add_row("chat_step_limit", str(cfg.max_steps))
        table.add_row("task_step_limit", str(cfg.task_max_steps))
        table.add_row("subagent_step_limit", str(cfg.subagent_max_steps))
    table.add_row("custom_tools_enabled", "yes" if cfg.custom_tools_enabled else "no")
    table.add_row("stream", "yes" if cfg.stream else "no")
    return table


def _provider_doctor_table(cfg: AppConfig) -> Table:
    diagnostics = build_provider_diagnostics(cfg)
    table = _Table(title="alysis doctor providers")
    table.add_column("field")
    table.add_column("value")
    for key, value in diagnostics.rows():
        table.add_row(key, value)
    last_call = last_provider_call_summary()
    if last_call:
        table.add_row("last_call_provider", str(last_call.get("provider_key") or "(unknown)"))
        table.add_row("last_call_protocol", str(last_call.get("protocol") or "(unknown)"))
        table.add_row("last_call_status", str(last_call.get("status_category") or "(unknown)"))
        table.add_row("last_call_latency_ms", str(last_call.get("latency_ms") or 0))
        table.add_row("last_call_stream", "yes" if last_call.get("stream") else "no")
        table.add_row(
            "last_call_web_search",
            str((last_call.get("web_search") or {}).get("backend_kind") or "off"),
        )
    last_search = last_web_search_summary()
    if last_search:
        table.add_row("last_web_search_adapter", str(last_search.get("web_search_adapter") or ""))
        table.add_row(
            "last_web_search_hosted",
            "yes" if last_search.get("provider_hosted_search") else "no",
        )
        table.add_row("last_web_search_sources", str(last_search.get("source_count") or 0))
    return table


def _auth_doctor_table() -> Table:
    payload = auth_doctor_payload()
    context = payload["context"]
    environment = payload["environment"]
    path = environment["path"]
    keyring = payload["keyring"]
    table = _Table(title="alysis doctor auth")
    table.add_column("check")
    table.add_column("result")
    table.add_row("context", str(context["kind"]))
    table.add_row(
        "tty",
        f"stdin={_yes_no(context['stdin_tty'])} stdout={_yes_no(context['stdout_tty'])} "
        f"stderr={_yes_no(context['stderr_tty'])}",
    )
    table.add_row("term", str(context["term"] or "(unset)"))
    table.add_row("ci_env", ", ".join(context["ci_env"]) or "(none)")
    table.add_row("home", str(environment["home"]))
    table.add_row("home_env", str(environment["home_env"] or "(unset)"))
    table.add_row("config_dir", str(environment["config_dir"]))
    table.add_row("config_dir_override", str(environment["config_dir_override"] or "(unset)"))
    table.add_row("config_exists", _yes_no(environment["config_exists"]))
    table.add_row(
        "path",
        f"{path['entry_count']} entries"
        + (" (looks minimal)" if path["looks_minimal"] else "")
        + f" · cli={path['resolved_cli'] or '(not on PATH)'}",
    )
    table.add_row("keyring_backend", str(keyring["backend"] or "(unresolved)"))
    table.add_row("keyring_available", _yes_no(keyring["available"]))
    table.add_row("keyring_env_override", str(keyring["env_override"] or "(unset)"))
    if not keyring["available"]:
        table.add_row("keyring_reason", str(keyring["reason"] or "(unknown)"))
    for store in payload["credential_stores"]:
        summary = "missing (no credentials stored yet)" if not store["exists"] else "readable"
        if store["exists"] and not store["readable"]:
            summary = f"UNREADABLE — {store['error']}"
        elif store["exists"]:
            summary = (
                f"readable · {store['entry_count']} entries · "
                f"key_source={store['key_source'] or '(unknown)'}"
            )
        table.add_row(f"store:{store['name']}", summary)
        table.add_row(f"store:{store['name']}:next_write", str(store["planned_key_source"]))
    return table


def _yes_no(value: object) -> str:
    return "yes" if value else "no"


def _doctor_bundle_payload(cfg: AppConfig) -> dict[str, Any]:
    diagnostics = build_provider_diagnostics(cfg)
    return diagnostic_bundle_payload(
        provider_diagnostics={key: value for key, value in diagnostics.rows()}
    )


def _provider_live_validation_table(cfg: AppConfig, *, timeout_s: float = 15.0) -> Table:
    validation = _patchable("validate_active_provider_live", validate_active_provider_live)(
        cfg,
        timeout_s=timeout_s,
    )
    table = _Table(title="alysis doctor providers --live")
    table.add_column("field")
    table.add_column("value")
    for key, value in validation.rows():
        table.add_row(key, value)
    return table


def _web_search_live_validation_table(cfg: AppConfig, *, timeout_s: float = 15.0) -> Table:
    validation = _patchable("validate_web_search_live", validate_web_search_live)(
        cfg,
        timeout_s=timeout_s,
    )
    table = _Table(title="web_search live check")
    table.add_column("field")
    table.add_column("value")
    for key, value in validation.rows():
        table.add_row(key, value)
    return table


def _reasoning_suppression_table(cfg: AppConfig, *, timeout_s: float = 15.0) -> Table:
    report = _patchable("probe_reasoning_suppression_live", probe_reasoning_suppression_live)(
        cfg,
        timeout_s=timeout_s,
    )
    table = _Table(title="reasoning-off live check")
    table.add_column("field")
    table.add_column("value")
    for key, value in report.rows():
        table.add_row(key, value)
    return table


@dataclass(frozen=True)
class _ToolAvailabilityRow:
    name: str
    categories: str
    status: str
    purpose: str
    notes: str


def _default_session_api_key(cfg: AppConfig | None = None) -> str | None:
    if cfg is not None:
        try:
            return resolve_model_access_api_key(cfg)
        except ConfigError:
            return None
    return _cli_module()._resolved_api_key_value().key


def _tool_availability_rows(cfg: AppConfig) -> list[_ToolAvailabilityRow]:
    rows: list[_ToolAvailabilityRow] = []
    main_api_key = _default_session_api_key(cfg)

    for spec in iter_builtin_tool_metadata():
        status = "available"
        notes: list[str] = []

        availability = get_tool_availability(spec.name)
        if spec.optional and availability is not None and availability.unavailable_reason:
            status = "optional-unavailable"
            notes.append(f"reason={availability.unavailable_reason}")

        if spec.name == "web_search":
            runtime = resolve_web_search_runtime_status(cfg=cfg, api_key=main_api_key)
            policy = resolve_web_search_policy(cfg)
            status = "policy-disabled" if policy == "off" else runtime.availability_label
            notes.append(f"policy={policy}")
            notes.append(f"mode={runtime.mode}")
            notes.append(f"provider={runtime.provider or '(none)'}")
            if runtime.registration_ready and policy != "off":
                notes.append("ready for registration in main agent sessions")
            elif runtime.registration_ready:
                notes.append("backend ready but web_search_policy=off prevents registration")
            if runtime.provider == "openai_responses":
                notes.append(
                    "OpenAI Responses readiness is conservative: explicit web_search_base_url or first-party OpenAI base_url"
                )
            elif runtime.provider in {
                "xai_responses",
                "anthropic_messages",
                "gemini_grounding",
                "openrouter_web",
                "perplexity_sonar",
                "groq_compound",
                "mistral_conversations",
                "moonshot_kimi",
                "zhipu_web_search",
                "volcengine_web_search",
                "minimax_coding_plan",
                "cohere_web_search",
            }:
                notes.append(f"available via {runtime.provider} provider adapter")
            elif runtime.provider == "dashscope_chat":
                notes.append(
                    "available via DashScope Chat Completions enable_search or Responses web_search"
                )
            elif runtime.provider == "tavily":
                notes.append("available via model-independent Tavily adapter")
            elif runtime.provider == "ddgs":
                notes.append("available via keyless DuckDuckGo metasearch (ddgs) adapter")
            notes.extend(runtime.notes)
            if policy == "off":
                notes.append("setup: set web_search_policy=auto to expose search to the model")
            elif not runtime.registration_ready:
                notes.append(f"setup: {runtime.setup_hint}")
        elif spec.name == "skill_read":
            if not resolve_skills_enabled(cfg):
                status = "optional-disabled"
                notes.append("set skills_enabled=true to advertise skills and register skill_read")
            else:
                status = "contextual"
                notes.append("registered only when discovered skill bundles are available")
        elif spec.name == "subagent_run" and not bool(getattr(cfg, "subagents_enabled", False)):
            status = "optional-disabled"
            notes.append("set subagents_enabled=true or use --subagents for top-level runs")

        if spec.built_in_subagent_exposure.strip().lower() == "hidden":
            notes.append("hidden from built-in readonly subagents")

        rows.append(
            _ToolAvailabilityRow(
                name=spec.name,
                categories=", ".join(spec.categories),
                status=status,
                purpose=spec.description.strip(),
                notes="; ".join(note for note in notes if note.strip()) or "-",
            )
        )
    return rows


def _tools_table(cfg: AppConfig) -> Table:
    table = _Table(title="alysis tools")
    table.add_column("tool")
    table.add_column("categories")
    table.add_column("status")
    table.add_column("purpose")
    table.add_column("notes")

    for row in _tool_availability_rows(cfg):
        table.add_row(
            row.name,
            row.categories,
            row.status,
            row.purpose,
            row.notes,
        )
    return table


@app.command()
def setup(
    section: str | None = typer.Argument(
        None,
        help="Optional setup target. Use `sandbox` to prepare the safe command runner.",
    ),
) -> None:
    """Run first-time setup, or prepare a named setup target."""
    if section is not None:
        target = section.strip().lower()
        if target != "sandbox":
            _console().print("[red]Unknown setup target.[/red] Use: alysis setup sandbox")
            raise typer.Exit(code=2)
        _cli_module()._run_sandbox_setup_command(pull=True)
        return

    from ..setup_wizard import run_setup_wizard

    # `alysis setup` defaults to the interactive alt-screen wizard (arrow-key
    # selection) regardless of ALYSIS_TUI; it only falls back to the classic
    # Rich wizard when the TUI can't run (and says why).
    tui_result = _cli_module()._try_setup_tui(require_flag=False, announce_fallback=True)
    if tui_result is None:
        # Classic Rich wizard (non-interactive terminal or TUI failure): configure
        # only, then surface any remaining sandbox setup.
        if run_setup_wizard():
            console = _console()
            configured = _patchable("load_config", load_config)()
            if configured.execution.backend == "delegated":
                return
            _cli_module()._maybe_run_startup_config_menu()
            configured = _patchable("load_config", load_config)()
            try:
                result = _patchable("diagnose_sandbox", diagnose_sandbox)(
                    configured,
                    include_smoke=False,
                    include_server_image=False,
                )
            except ConfigError:
                return
            if not result.ready:
                console.print()
                console.print(
                    "[yellow]Safe runner setup is not complete yet. "
                    "Run `alysis doctor sandbox` for details or `alysis setup sandbox` "
                    "after installing/starting Docker.[/yellow]"
                )
        return

    # The interactive TUI ran (it already walked the user through sandbox). On a
    # saved setup, flow straight into chat — the final screen promised "Press
    # Enter to start chatting" — in the workspace they just configured (so the
    # broad-workspace guard does not re-ask about it). On cancel, back to shell.
    if tui_result:
        configured = _patchable("load_config", load_config)()
        if configured.execution.backend == "delegated":
            return
        _cli_module()._maybe_run_startup_config_menu()
        _cli_module()._run_chat_after_setup()


@app.command()
def doctor(
    section: str | None = typer.Argument(
        None,
        help="Optional check group. Use `sandbox`, `providers`, `auth`, or `bundle`.",
    ),
    smoke: bool = typer.Option(
        True,
        "--smoke/--no-smoke",
        help="Run a sandbox smoke command when checking `doctor sandbox`.",
    ),
    env: bool = typer.Option(
        False,
        "--env",
        help="Show sandbox-related environment variable overrides.",
    ),
    live: bool = typer.Option(
        False,
        "--live",
        help="Run a minimal live text request for `doctor providers` after confirmation.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Confirm live provider validation without prompting.",
    ),
    live_timeout: float = typer.Option(
        15.0,
        "--live-timeout",
        min=1.0,
        help="Timeout in seconds for `doctor providers --live`.",
    ),
    redacted: bool = typer.Option(
        True,
        "--redacted/--no-redacted",
        help="Emit only redacted diagnostic data for `doctor bundle`.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help=(
            "Emit `doctor auth` as one machine-readable JSON object on stdout. "
            "Rejected for the check groups that have no JSON form."
        ),
    ),
) -> None:
    if section is not None:
        target = section.strip().lower()
        if target == "auth":
            if json_output is True:
                typer.echo(json.dumps(auth_doctor_payload(), sort_keys=True))
                return
            _console().print(_auth_doctor_table())
            return
        if json_output is True and target != "bundle":
            # Silently printing a table to a caller that asked for JSON would hand
            # it unparseable output. `bundle` is already JSON-only.
            _console().print(
                "[red]--json is only supported for `doctor auth` and `doctor bundle`.[/red]"
            )
            raise typer.Exit(code=2)
        if target == "sandbox":
            _cli_module()._run_sandbox_doctor_command(include_smoke=smoke, include_env=env)
            return
        if target in {"provider", "providers"}:
            cfg = _patchable("load_config", load_config)()
            console = _console()
            console.print(_provider_doctor_table(cfg))
            if live:
                _require_active_subscription_ready(model=None, base_url=None)
                console.print(
                    "[yellow]Live provider validation sends a few minimal requests (one text "
                    "request, one reasoning-off request, and one web search when enabled) and "
                    "may incur provider cost or rate-limit usage.[/yellow]"
                )
                if not yes and not typer.confirm(
                    "Run live provider validation for the active profile?", default=False
                ):
                    console.print("Live provider validation cancelled.")
                    return
                console.print(_provider_live_validation_table(cfg, timeout_s=live_timeout))
                console.print(_reasoning_suppression_table(cfg, timeout_s=live_timeout))
                console.print(_web_search_live_validation_table(cfg, timeout_s=live_timeout))
            return
        if target == "bundle":
            if not redacted:
                _console().print("[red]Only redacted doctor bundles are supported.[/red]")
                raise typer.Exit(code=2)
            cfg = _patchable("load_config", load_config)()
            typer.echo(json.dumps(_doctor_bundle_payload(cfg), indent=2, sort_keys=True))
            return
        if target != "sandbox":
            _console().print(
                "[red]Unknown doctor target.[/red] Use: alysis doctor sandbox|providers|auth|bundle"
            )
            raise typer.Exit(code=2)
    if json_output is True:
        _console().print(
            "[red]--json is only supported for `doctor auth` and `doctor bundle`.[/red]"
        )
        raise typer.Exit(code=2)
    console = _console()
    cfg = _patchable("load_config", load_config)()
    console.print(_doctor_table(cfg))


@app.command()
def tools() -> None:
    console = _console()
    cfg = _patchable("load_config", load_config)()
    console.print(_tools_table(cfg))
    console.print(
        "[dim]`alysis tools` shows the built-in catalog plus config-dependent availability. "
        "Use `/status` inside chat for session-specific details.[/dim]"
    )
    console.print(
        "[dim]`web_search` discovers candidate sources; `web_fetch` retrieves a specific chosen URL.[/dim]"
    )
    console.print(
        "[dim]Top-level readonly/Plan sessions can use ready web tools; nested readonly subagents keep them hidden.[/dim]"
    )
    console.print(
        "[dim]Use `web_search_policy=off|auto` for model web-search access, "
        "`web_search_mode=off|auto|native|external` for backend selection, and optional "
        "`web_search_adapter`. "
        "`auto` can use OpenAI Responses, xAI, Anthropic, Gemini, OpenRouter, DashScope "
        "Chat/Qwen, Kimi, Zhipu/GLM, Doubao, Perplexity, Groq, Mistral, or Tavily when "
        "`ALYSIS_WEB_SEARCH_API_KEY` or `TAVILY_API_KEY` is set. `native` never uses "
        "Tavily; `external` uses only external "
        "search adapters. Legacy `on` and `web_search_enabled` values still load as `auto`.[/dim]"
    )
    console.print(
        "[dim]Custom tools are managed separately via `alysis tool list|info|trust|untrust`.[/dim]"
    )


@app.command()
def login() -> None:
    """Connect your Alysis Code account."""
    from ... import account_login

    console = _console()
    cfg = _patchable("load_config", load_config)()
    try:
        result = account_login.login(
            cfg, output_write=lambda message: console.print(message, highlight=False)
        )
    except (account_login.AlysisLoginError, ConfigError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    who = f" as [bold]{result.email}[/bold]" if result.email else ""
    console.print(f"[green]Logged in{who}.[/green] Your Alysis Code account is connected.")
    if result.model:
        console.print(
            f"Active profile: [bold]{result.profile_name}[/bold] · default model: "
            f"[bold]{result.model}[/bold]"
        )
        console.print("[dim]Switch model anytime with /model in chat, or `alysis config`.[/dim]")
    else:
        # No model is auto-selected on first login (the free MiMo default was
        # removed); guide the user to pick one before the "Model is not set" guard.
        console.print(
            f"Active profile: [bold]{result.profile_name}[/bold] · no model selected yet."
        )
        console.print(
            "[dim]Pick one with /model in chat, or `alysis config set model <MODEL>`.[/dim]"
        )
    console.print("[dim]Run `alysis chat` to start. Use `alysis logout` to disconnect.[/dim]")


@app.command()
def logout() -> None:
    """Disconnect your Alysis Code account (forgets the stored access key)."""
    from ... import account_login

    console = _console()
    cfg = _patchable("load_config", load_config)()
    if account_login.logout(cfg):
        console.print("[green]Logged out.[/green] Your stored Alysis Code key was removed.")
    else:
        console.print("You're not logged in to an Alysis Code account.")


def _whoami_payload(cfg: AppConfig) -> dict[str, Any]:
    """Build the Alysis Code account status contract for a supervising app."""
    from ... import account_login

    status = account_login.login_status(cfg)
    trial = account_login.fetch_trial_status(cfg) if status.logged_in else None
    detail: str | None = None
    if not status.logged_in:
        detail = "No Alysis Code account is connected."
    elif trial is None:
        from ...alysis_cloud import site_host

        detail = f"Connected. Manage your plan and credits at {site_host()}/account."
    else:
        detail = account_login.format_trial_status_line(trial) or None
    return _auth_status_payload(
        connection="alysis",
        authenticated=status.logged_in,
        account_label=(trial.email if trial is not None else None),
        method=("access-key" if status.logged_in else None),
        detail=detail,
        transport=status.base_url,
        profile_name=status.profile_name,
        profile_active=bool(status.active),
        plan=(trial.plan if trial is not None else None),
        display_name=(trial.display_name if trial is not None else None),
        billing=(trial.billing if trial is not None else None),
        billing_status=(
            "available"
            if trial is not None and trial.billing is not None
            else "unavailable"
            if status.logged_in
            else "signed_out"
        ),
        account_url=account_login.cloud.account_url(),
    )


@app.command()
def whoami(
    json_output: bool = typer.Option(False, "--json", help=_JSON_OPTION_HELP),
) -> None:
    """Show your Alysis Code login status."""
    from ... import account_login

    console = _console()
    cfg = _patchable("load_config", load_config)()
    if json_output is True:
        typer.echo(json.dumps(_whoami_payload(cfg), sort_keys=True))
        return
    status = account_login.login_status(cfg)
    if not status.logged_in:
        console.print("Not logged in. Run `alysis login` to connect your Alysis Code account.")
        return
    active = "active" if status.active else "not active"
    console.print("[green]Logged in[/green] to your Alysis Code account.")
    console.print(
        f"Profile: [bold]{status.profile_name}[/bold] ({active}) · key {status.key_preview}"
    )
    console.print(f"Proxy: {status.base_url}")
    trial = account_login.fetch_trial_status(cfg)
    if trial is not None:
        line = account_login.format_trial_status_line(trial)
        if line:
            console.print(line)
    else:
        console.print("[dim](Live account usage is unavailable. Check your account website.)[/dim]")
