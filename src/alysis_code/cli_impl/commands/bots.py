from __future__ import annotations

import json

import typer

bots_app = typer.Typer(add_completion=False, help="Private local Bots service.")


@bots_app.command()
def serve() -> None:
    """Run the desktop-owned service, bootstrapped over its private stdin pipe."""
    from ...bots.local_service import main

    main()


@bots_app.command()
def profiles() -> None:
    """List saved model connections without exposing credentials."""
    from ...bots.local_service import profiles as payload

    typer.echo(json.dumps(payload()))


@bots_app.command("cloud-access")
def cloud_access(connect: bool = False) -> None:
    """Read account access. Connection credentials are sealed to the native caller."""
    from ...bots.cloud_account import desktop_access

    typer.echo(json.dumps(desktop_access(connect=connect)))
