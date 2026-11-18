"""CLI startup for the desktop's version, capability and IDE bridge invocations."""

from __future__ import annotations

import json

import typer

from .. import __version__
from ..build_identity import version_line
from .commands._entry import _cli_module, _enforce_clean_build


def capabilities() -> None:
    """Emit the credential-free local surface capability contract."""

    from ..capabilities import build_capabilities_payload

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


def create_desktop_app() -> typer.Typer:
    from .commands.ide_bridge import ide_bridge_app

    app = typer.Typer(add_completion=False)
    app.command()(capabilities)
    app.add_typer(ide_bridge_app, name="ide-bridge")

    @app.callback(invoke_without_command=True)
    def main(
        version: bool = typer.Option(False, "--version", callback=_show_version, is_eager=True),
    ) -> None:
        _enforce_clean_build(False)
        _cli_module()

    return app
