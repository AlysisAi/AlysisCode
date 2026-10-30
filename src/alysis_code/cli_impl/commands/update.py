from __future__ import annotations

import json
from typing import Any

import typer

from ... import __version__
from ...config import AppConfig, ConfigError, load_config
from ...updates import (
    InstallerPlan,
    UpdateStatus,
    check_for_updates,
    detect_installer_plan,
    maybe_refresh_update_cache_in_background,
    passive_update_notice,
    read_update_prompt_state,
    record_update_prompt_shown,
    record_update_skipped,
    resolve_update_check_enabled,
    run_installer_plan,
    should_prompt_for_update,
    status_from_cache,
)
from . import _patchable
from ._shared import _console

update_app = typer.Typer(add_completion=False, help="Check for and apply Alysis Code updates.")
_update_prompt_completed = False


def _cached_update_notice() -> str | None:
    try:
        return passive_update_notice(
            current_version=__version__, cfg=_patchable("load_config", load_config)()
        )
    except Exception:  # noqa: BLE001 - update notices must never block startup/status rendering
        return None


def _start_background_update_check() -> None:
    try:
        maybe_refresh_update_cache_in_background(
            current_version=__version__,
            cfg=_patchable("load_config", load_config)(),
        )
    except Exception:  # noqa: BLE001 - update checks must never block normal CLI startup
        return


def _classic_update_prompt(console: Any, status: UpdateStatus, plan: InstallerPlan) -> str:
    console.print(
        f"[yellow]Alysis Code {status.latest_version} is available.[/yellow] "
        f"You have {status.current_version}."
    )
    if status.url:
        console.print(f"[dim]Release:[/dim] {status.url}")
    if plan.supported and plan.display_command:
        console.print(f"[dim]Update command:[/dim] {plan.display_command}")
    elif plan.reason:
        console.print(f"[dim]{plan.reason}[/dim]")
    try:
        answer = (
            typer.prompt("Update now? [y=update / n=later / s=skip this version]", default="n")
            .strip()
            .lower()
        )
    except (EOFError, KeyboardInterrupt, typer.Abort):
        # typer.prompt surfaces Ctrl-C/Ctrl-D as typer.Abort.
        return "later"
    if answer in {"y", "yes", "u", "update"}:
        return "update"
    if answer in {"s", "skip"}:
        return "skip"
    return "later"


def _tui_popup_enabled() -> bool:
    try:
        from ..tui.config import is_tui_enabled

        return bool(is_tui_enabled())
    except Exception:  # noqa: BLE001 - TUI probing must not break the prompt
        return False


def _pause_before_alt_screen(console: Any) -> None:
    """Keep launch-time guidance readable before the chat TUI covers it."""
    if not _tui_popup_enabled():
        return
    try:
        typer.prompt("Press Enter to continue", default="", show_default=False)
    except (EOFError, KeyboardInterrupt, typer.Abort):
        pass


def _prompt_update_choice(console: Any, status: UpdateStatus, plan: InstallerPlan) -> str:
    if _tui_popup_enabled():
        try:
            from ..tui.update_prompt import select_update_action

            choice, interactive_available = select_update_action(
                current_version=status.current_version,
                latest_version=str(status.latest_version),
                command=plan.display_command if plan.supported else None,
                unsupported_reason=None if plan.supported else (plan.reason or None),
            )
            if interactive_available:
                return choice or "later"
        except Exception:  # noqa: BLE001 - fall back to the classic inline prompt
            pass
    return _classic_update_prompt(console, status, plan)


def _apply_startup_update(console: Any, plan: InstallerPlan) -> str:
    """Run the detected upgrade command; exits the process on success."""
    _print_installer_plan(console, plan)
    if not plan.supported:
        console.print(
            "[yellow]Automatic update is not available for this install.[/yellow] "
            "Update manually using the installer that created this environment."
        )
        _pause_before_alt_screen(console)
        return "manual"
    try:
        exit_code = _patchable("run_installer_plan", run_installer_plan)(plan)
    except Exception as exc:  # noqa: BLE001 - a failed upgrade must not block launch
        console.print(f"[red]Update failed:[/red] {exc} Continuing with the current version.")
        _pause_before_alt_screen(console)
        return "update-failed"
    if exit_code != 0:
        console.print(
            f"[red]Update command failed with exit code {exit_code}.[/red] "
            "Continuing with the current version."
        )
        _pause_before_alt_screen(console)
        return "update-failed"
    console.print("[green]Update installed.[/green] Restart Alysis Code to use the new version.")
    raise typer.Exit(code=0)


def maybe_prompt_update_at_startup(*, console: Any | None = None) -> str | None:
    """Offer a newer release at launch, before the setup/workspace screens.

    Runs at most once per process and decides from the cached check only, so
    it never blocks startup on the network. Returns the action taken
    ("update-failed" / "manual" / "skip" / "later"), or None when no prompt was
    shown; a successful update exits the process via ``typer.Exit`` so the
    user restarts into the new version.
    """
    global _update_prompt_completed
    if _update_prompt_completed:
        return None
    _update_prompt_completed = True

    try:
        from .startup import _is_interactive_terminal

        if not _is_interactive_terminal():
            return None
        cfg = _patchable("load_config", load_config)()
        status = _patchable("status_from_cache", status_from_cache)(
            current_version=__version__,
            cfg=cfg,
        )
        state = read_update_prompt_state()
        if not should_prompt_for_update(status=status, state=state, cfg=cfg):
            return None
        latest = status.latest_version
        if not latest:
            return None
        plan = _patchable("detect_installer_plan", detect_installer_plan)()
        if plan.method == "editable":
            # Dev checkouts update via git; don't nag at every launch.
            return None
        record_update_prompt_shown(latest)
        out = console if console is not None else _console()
        choice = _prompt_update_choice(out, status, plan)
    except Exception:  # noqa: BLE001 - the update prompt must never break startup
        return None

    if choice == "skip":
        try:
            record_update_skipped(latest)
        except Exception:  # noqa: BLE001 - persisting the skip is best-effort
            pass
        out.print(f"[dim]Skipping version {latest}. Run `alysis update` any time.[/dim]")
        return "skip"
    if choice != "update":
        return "later"
    return _apply_startup_update(out, plan)


def _print_update_status(console: Any, status: UpdateStatus) -> None:
    if status.source == "disabled":
        console.print("Update checks are disabled in config.")
        return
    if status.update_available and status.latest_version:
        console.print(
            f"[yellow]Alysis Code {status.latest_version} is available.[/yellow] "
            f"You have {status.current_version}."
        )
        if status.url:
            console.print(f"[dim]Release:[/dim] {status.url}")
        console.print("[dim]Run `alysis update` to apply it with confirmation.[/dim]")
        return
    if status.up_to_date:
        console.print(f"Alysis Code is up to date ({status.current_version}).")
        return
    if status.error:
        console.print(f"[red]Update check failed:[/red] {status.error}")
        return
    console.print("No update check has been recorded yet. Run `alysis update check`.")


def _update_status_or_exit(
    *,
    console: Any,
    cfg: AppConfig,
    cached: bool,
) -> UpdateStatus:
    if cached:
        status = _patchable("status_from_cache", status_from_cache)(
            current_version=__version__,
            cfg=cfg,
        )
    else:
        status = _patchable("check_for_updates", check_for_updates)(
            current_version=__version__,
            cfg=cfg,
            force=True,
        )
    if status.error:
        _print_update_status(console, status)
        raise typer.Exit(code=1)
    return status


def _print_installer_plan(console: Any, plan: InstallerPlan) -> None:
    console.print(f"[dim]Installer:[/dim] {plan.method}")
    if plan.reason:
        console.print(f"[dim]Reason:[/dim] {plan.reason}")
    if plan.display_command:
        console.print(f"[dim]Command:[/dim] {plan.display_command}")


def _run_update_flow(*, yes: bool, dry_run: bool, cached: bool) -> None:
    console = _console()
    cfg = _patchable("load_config", load_config)()
    try:
        status = _update_status_or_exit(console=console, cfg=cfg, cached=cached)
    except ConfigError as exc:
        console.print(f"[red]Update config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    _print_update_status(console, status)
    if not status.update_available:
        return

    plan = _patchable("detect_installer_plan", detect_installer_plan)()
    _print_installer_plan(console, plan)
    if not plan.supported:
        console.print(
            "[yellow]Automatic command selection is not available for this install.[/yellow]"
        )
        console.print("Update manually using the installer that created this environment.")
        raise typer.Exit(code=1)
    if dry_run:
        console.print("[dim]Dry run only; no command executed.[/dim]")
        return
    if not yes and not typer.confirm("Run this update command now?", default=False):
        console.print("Update cancelled.")
        return
    exit_code = _patchable("run_installer_plan", run_installer_plan)(plan)
    if exit_code != 0:
        console.print(f"[red]Update command failed with exit code {exit_code}.[/red]")
        raise typer.Exit(code=exit_code)
    console.print("[green]Update command completed.[/green] Restart Alysis Code to use it.")


@update_app.callback(invoke_without_command=True)
def update_main(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="Run the detected update command."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show the detected update command without running it.",
    ),
    cached: bool = typer.Option(
        False,
        "--cached",
        help="Use cached version information instead of checking PyPI first.",
    ),
) -> None:
    """Check for a newer Alysis Code release and apply it only after confirmation."""
    if ctx.invoked_subcommand is not None:
        return
    _run_update_flow(yes=yes, dry_run=dry_run, cached=cached)


@update_app.command("check")
def update_check(
    cached: bool = typer.Option(
        False,
        "--cached",
        help="Use cached version information instead of checking PyPI.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Check whether a newer Alysis Code package is available."""
    console = _console()
    cfg = _patchable("load_config", load_config)()
    try:
        if cached:
            status = _patchable("status_from_cache", status_from_cache)(
                current_version=__version__,
                cfg=cfg,
            )
        else:
            status = _patchable("check_for_updates", check_for_updates)(
                current_version=__version__,
                cfg=cfg,
                force=True,
            )
    except ConfigError as exc:
        console.print(f"[red]Update config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    if as_json:
        console.print_json(json.dumps(status.to_json()))
        if status.error:
            raise typer.Exit(code=1)
        return
    _print_update_status(console, status)
    if status.error:
        raise typer.Exit(code=1)


def _update_status_summary(status: UpdateStatus, cfg: AppConfig | None) -> str:
    if not resolve_update_check_enabled(cfg):
        return "disabled"
    if status.update_available and status.latest_version:
        return f"available {status.latest_version} (run: alysis update)"
    if status.up_to_date and status.latest_version:
        return f"current ({status.current_version})"
    if status.error:
        return f"last check failed ({status.error})"
    return "not checked (run: alysis update check)"


def _cached_update_status_summary(cfg: AppConfig | None) -> str:
    try:
        return _update_status_summary(
            _patchable("status_from_cache", status_from_cache)(
                current_version=__version__,
                cfg=cfg,
            ),
            cfg,
        )
    except ConfigError as exc:
        return f"config error ({exc})"
    except Exception:
        return "unknown"
