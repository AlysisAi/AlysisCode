"""Narrow startup path for the app's fresh, JSON-only account probes."""

from __future__ import annotations

from collections.abc import Sequence

import typer


def is_auth_status_probe(args: Sequence[str]) -> bool:
    # Leave all other parsing, options, help and commands to the full CLI.
    return (
        len(args) == 4
        and args[0] == "auth"
        and args[1] == "status"
        and bool(args[2])
        and not args[2].startswith("-")
        and args[3] == "--json"
    )


def create_auth_status_app() -> typer.Typer:
    from .commands._entry import _cli_module, _enforce_clean_build
    from .commands.auth import auth_app

    app = typer.Typer(add_completion=False, help="Local CLI coding agent (multi-provider).")
    # Register the existing command group: argument parsing, status validation,
    # secure-store reads, refresh, JSON, errors and cancellation stay there.
    app.add_typer(auth_app, name="auth")

    @app.callback(invoke_without_command=True)
    def main(ctx: typer.Context) -> None:
        # The admitted argv has no root options. Preserve root.main's startup
        # checks and context reset, including ALYSIS_REQUIRE_CLEAN_BUILD and
        # canonical facade initialization for the `python -m ...cli` entry.
        _enforce_clean_build(False)
        _cli_module()

    return app
