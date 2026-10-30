from __future__ import annotations

import json
from enum import Enum
from typing import Any

import typer


def health_payload() -> dict[str, Any]:
    # Health discovery loads the IDE protocol's event and management schemas.
    # Other CLI commands need only this command's registration.
    from ...ide.health import health_payload as build

    return build()


def run_stdio_bridge(*, role: str = "runtime") -> int:
    # Registering the CLI must not construct the IDE's execution dependency graph.
    from ...ide.stdio_bridge import run_stdio_bridge as run

    return run() if role == "runtime" else run(role=role)


ide_bridge_app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    help="Run the Alysis Code IDE protocol bridge.",
)


class BridgeRole(str, Enum):
    runtime = "runtime"
    projection = "plugin-projection"


@ide_bridge_app.callback(invoke_without_command=True)
def ide_bridge(
    ctx: typer.Context,
    role: BridgeRole = typer.Option(
        BridgeRole.runtime, "--role", help="Immutable bridge process role."
    ),
    stdio: bool = typer.Option(False, "--stdio", help="Run protocol v1 over stdio JSONL."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if stdio:
        raise typer.Exit(
            code=run_stdio_bridge()
            if role == BridgeRole.runtime
            else run_stdio_bridge(role="plugin_projection")
        )
    typer.echo(ctx.get_help())


@ide_bridge_app.command("health")
def ide_bridge_health() -> None:
    typer.echo(json.dumps(health_payload(), ensure_ascii=True, sort_keys=True))
