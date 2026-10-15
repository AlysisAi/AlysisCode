from __future__ import annotations

import json
from pathlib import Path

import typer

from ...config import ConfigError, load_config
from ...subagent_settings import set_subagent_model, subagent_catalog
from . import _patchable
from ._shared import _console, _resolve_tool_workspace_root, _Table

agents_app = typer.Typer(
    add_completion=False,
    help="Subagents and the model each one uses.",
)

_PATH_HELP = "Project folder whose .alysis_agents/ is included. Default: the current folder."
_JSON_HELP = "Print JSON."


def _root(path: Path | None) -> Path | None:
    try:
        return _resolve_tool_workspace_root(path=path if path is not None else Path("."))
    except Exception:  # noqa: BLE001 - outside a usable project: built-ins and your own only
        if path is not None:
            raise
        return None


@agents_app.command("list")
def agents_list(
    path: Path | None = typer.Option(None, "--path", help=_PATH_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """List subagents with the model and provider profile each one would run on."""

    cfg = _patchable("load_config", load_config)()
    payload = subagent_catalog(cfg, root=_root(path))
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    table = _Table(title="Subagents")
    table.add_column("Name")
    table.add_column("Model")
    table.add_column("Profile")
    table.add_column("Status")
    for agent in payload["agents"]:
        model = agent["effective_model"] or "-"
        if agent["model"]:
            model = f"{model} (set)"
        status = "ready"
        if not agent["available"]:
            status = str((agent["unavailable"] or {}).get("reason") or "unavailable")
        elif agent["problem"]:
            status = str(agent["problem"])
        table.add_row(agent["name"], model, agent["effective_profile"] or "-", status)
    console = _console()
    console.print(table)
    if not payload["subagents_enabled"]:
        console.print("Subagents are turned off (subagents_enabled = false).")


@agents_app.command("set")
def agents_set(
    name: str = typer.Argument(..., help="Subagent name, for example explorer."),
    model: str = typer.Option("", "--model", help="Model id. Leave out to use the default."),
    profile: str = typer.Option(
        "", "--profile", help="Provider profile to run it on. Leave out for the active one."
    ),
    clear: bool = typer.Option(False, "--clear", help="Remove the saved model and profile."),
    path: Path | None = typer.Option(None, "--path", help=_PATH_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Save the model (and provider profile) one subagent uses, in your agents/ folder.

    --model and --profile replace the saved choice; whatever you leave out falls back to
    the subagent's default. Applies to new chats.
    """

    if clear and (model.strip() or profile.strip()):
        raise typer.BadParameter("Use --clear alone, or give --model and/or --profile.")
    if not clear and not model.strip() and not profile.strip():
        raise typer.BadParameter("Give --model and/or --profile, or --clear.")
    cfg = _patchable("load_config", load_config)()
    try:
        result = set_subagent_model(
            cfg,
            name,
            model=None if clear else model,
            profile=None if clear else profile,
            root=_root(path),
        )
    except ConfigError as exc:
        _console().print(f"[red]Not saved:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    if json_output:
        typer.echo(json.dumps(result, indent=2))
        return
    agent = result["agent"]
    uses = agent["effective_model"] or "the default model"
    profile_text = f" on {agent['effective_profile']}" if agent["effective_profile"] else ""
    _console().print(f"Saved. {agent['name']} uses {uses}{profile_text} in new chats.")
