from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

from ...config import ConfigError, load_config
from ...sandbox_doctor import (
    SandboxDiagnostic,
    configured_sandbox_images,
    diagnose_sandbox,
    format_sandbox_problem_message,
    pull_sandbox_images,
    sandbox_env_summary,
)
from . import _patchable
from ._shared import _console, _Table

if TYPE_CHECKING:
    from rich.table import Table


sandbox_app = typer.Typer(add_completion=False, help="Sandbox setup and diagnostics.")


def _sandbox_doctor_table(result: SandboxDiagnostic) -> Table:
    table = _Table(title="alysis sandbox")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    table.add_row("mode", result.configured_mode, "shell_sandbox.mode")
    table.add_row("configured backend", result.configured_backend, "shell_sandbox.backend")
    table.add_row("selected backend", result.selected_backend or "-", "")
    if result.selected_backend not in {"windows-native", "windows-mxc"}:
        table.add_row("sandbox image", result.docker_image, "dev shell/verify image")
        table.add_row("server image", result.server_image, "server worker image")
    for check in result.checks:
        table.add_row(check.name, check.status, check.detail)
    return table


def _print_sandbox_diagnostic(
    *,
    console: Any,
    result: SandboxDiagnostic,
    include_env: bool = False,
) -> None:
    console.print(_sandbox_doctor_table(result))
    console.print()
    message_style = "green" if result.ready else "yellow"
    console.print(format_sandbox_problem_message(result), style=message_style, highlight=False)
    if include_env:
        env_rows = sandbox_env_summary()
        env_table = _Table(title="sandbox environment")
        env_table.add_column("variable")
        env_table.add_column("value")
        for key, value in env_rows.items():
            env_table.add_row(key, value if value is not None else "(unset)")
        console.print()
        console.print(env_table)


def _run_sandbox_doctor_command(*, include_smoke: bool, include_env: bool = False) -> None:
    console = _console()
    try:
        result = _patchable("diagnose_sandbox", diagnose_sandbox)(
            _patchable("load_config", load_config)(),
            include_smoke=include_smoke,
            include_server_image=True,
        )
    except ConfigError as exc:
        console.print(f"[red]Sandbox config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    _print_sandbox_diagnostic(console=console, result=result, include_env=include_env)
    if not result.ready:
        raise typer.Exit(code=1)


def _run_sandbox_pull_command(
    *,
    images: list[str] | None = None,
    include_server: bool = True,
    timeout_s: int = 900,
) -> None:
    console = _console()
    if images:
        selected_images = tuple(images)
    else:
        try:
            selected_images = _patchable("configured_sandbox_images", configured_sandbox_images)(
                _patchable("load_config", load_config)(),
                include_server=include_server,
            )
        except ConfigError as exc:
            console.print(f"[red]Sandbox config error:[/red] {exc}")
            raise typer.Exit(code=2) from exc
    result = _patchable("pull_sandbox_images", pull_sandbox_images)(
        selected_images, timeout_s=timeout_s
    )
    if result.error:
        console.print(f"[red]Sandbox setup failed:[/red] {result.error}")
        raise typer.Exit(code=1)
    for item in result.results:
        status = "pulled" if item.ok else "failed"
        style = "green" if item.ok else "red"
        console.print(f"[{style}]{status}[/{style}] {item.image}")
        if not item.ok and item.output:
            console.print(item.output)
    if not result.ok:
        raise typer.Exit(code=1)
    _run_sandbox_doctor_command(include_smoke=True, include_env=False)


def _run_sandbox_setup_command(*, pull: bool = True) -> None:
    console = _console()
    try:
        result = _patchable("diagnose_sandbox", diagnose_sandbox)(
            _patchable("load_config", load_config)(),
            include_smoke=False,
            include_server_image=True,
        )
    except ConfigError as exc:
        console.print(f"[red]Sandbox config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    if result.ready:
        _print_sandbox_diagnostic(console=console, result=result)
        return
    if pull and result.can_pull:
        console.print("[bold]Downloading Alysis Code sandbox images...[/bold]")
        _patchable("_run_sandbox_pull_command", _run_sandbox_pull_command)(include_server=True)
        return
    _print_sandbox_diagnostic(console=console, result=result)
    raise typer.Exit(code=1)


@sandbox_app.command("doctor")
def sandbox_doctor(
    smoke: bool = typer.Option(
        True,
        "--smoke/--no-smoke",
        help="Run a sandbox smoke command.",
    ),
    env: bool = typer.Option(
        False,
        "--env",
        help="Show sandbox-related environment variable overrides.",
    ),
) -> None:
    """Check whether Alysis Code's safe command runner is ready."""
    _run_sandbox_doctor_command(include_smoke=smoke, include_env=env)


@sandbox_app.command("setup")
def sandbox_setup(
    native_windows: bool = typer.Option(
        False,
        "--native-windows",
        help="Install the experimental native Windows sandbox account and network rules (one UAC prompt).",
    ),
    pull: bool = typer.Option(
        True,
        "--pull/--no-pull",
        help="Download missing Docker sandbox images when Docker is ready.",
    ),
) -> None:
    """Prepare Alysis Code's safe command runner."""
    if native_windows:
        import subprocess

        from ...windows_sandbox import resolve_helper

        try:
            helper = resolve_helper()
        except ConfigError as exc:
            _console().print(str(exc), style="red", markup=False)
            raise typer.Exit(code=2) from exc
        _console().print(
            "Installing the experimental Alysis Windows sandbox. Windows will request administrator consent. Commands use isolated AppContainers with an independent filesystem write restriction."
        )
        result = subprocess.run([str(helper), "install"], check=False)
        if result.returncode:
            raise typer.Exit(code=result.returncode)
        _console().print(
            "Setup completed. Select windows-native with network=off and run alysis sandbox doctor --smoke. This preview is not release-qualified."
        )
        return
    _run_sandbox_setup_command(pull=pull)


@sandbox_app.command("native-status")
def sandbox_native_status() -> None:
    """Print native setup status as JSON without changing machine configuration."""
    import json

    from ...windows_sandbox import helper_status

    try:
        result = helper_status()
    except ConfigError as exc:
        _console().print(str(exc), style="red", markup=False)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(result))


@sandbox_app.command("native-capabilities")
def sandbox_native_capabilities() -> None:
    """Print engine compatibility as JSON; does not install or probe the helper."""
    import json

    from ...windows_sandbox import engine_capabilities

    typer.echo(json.dumps(engine_capabilities()))


@sandbox_app.command("mxc-capabilities")
def sandbox_mxc_capabilities() -> None:
    """Print the separate MXC candidate contract, without setup or readiness claims."""
    import json

    from ...windows_mxc_contract import engine_capabilities

    typer.echo(json.dumps(engine_capabilities()))


@sandbox_app.command("recover-native")
def sandbox_recover_native() -> None:
    """Recover this user's native ACL journal after interrupted execution."""
    import subprocess

    from ...windows_sandbox import resolve_helper

    try:
        helper = resolve_helper()
    except ConfigError as exc:
        _console().print(str(exc), style="red", markup=False)
        raise typer.Exit(code=1) from exc
    result = subprocess.run([str(helper), "recover"], check=False)
    if result.returncode:
        raise typer.Exit(code=result.returncode)


@sandbox_app.command("pull")
def sandbox_pull(
    image: list[str] | None = typer.Option(
        None,
        "--image",
        help="Specific sandbox image to pull. Repeat to pull more than one.",
    ),
    include_server: bool = typer.Option(
        True,
        "--server/--no-server",
        help="Also pull the server worker sandbox image.",
    ),
    timeout_s: int = typer.Option(
        900,
        "--timeout",
        min=1,
        help="Per-image Docker pull timeout in seconds.",
    ),
) -> None:
    """Download Alysis Code's Docker sandbox image(s)."""
    _run_sandbox_pull_command(images=image, include_server=include_server, timeout_s=timeout_s)
