from __future__ import annotations

import json
import os
import sys

import typer

from ...branding import env_get
from ...config import (
    ConfigError,
    clear_persisted_api_key,
    config_path,
    credentials_path,
    ensure_subscription_menu_managed_key,
    load_config,
    resolve_api_key,
    save_config,
    save_persisted_api_key,
    set_config_value,
)
from ...llm.metadata import credential_scope_fingerprint
from . import _patchable
from ._shared import _console

config_app = typer.Typer(add_completion=False, help="Configuration commands.")


@config_app.command("show")
def config_show() -> None:
    console = _console()
    cfg = _patchable("load_config", load_config)()
    data = cfg.model_dump()
    data["config_path"] = os.fspath(config_path())
    api_key = _patchable("resolve_api_key", resolve_api_key)(cfg)
    data["api_key_set"] = bool(api_key.key)
    data["api_key_source"] = api_key.source
    # Match the live route identity used by the Computer Use IDE boundary while
    # keeping the credential itself outside this machine-readable snapshot.
    data["credential_id"] = credential_scope_fingerprint(api_key.key)
    data["credentials_path"] = os.fspath(credentials_path())
    data["active_profile"] = str((cfg.extra_fields or {}).get("active_profile") or "")
    role_models = (cfg.extra_fields or {}).get("role_models")
    forge_role_models = (cfg.extra_fields or {}).get("forge_role_models")
    data["role_models"] = dict(role_models) if isinstance(role_models, dict) else {}
    data["forge_role_models"] = (
        dict(forge_role_models) if isinstance(forge_role_models, dict) else {}
    )
    console.print_json(json.dumps(data))


@config_app.command("menu")
def config_menu_cmd() -> None:
    """Open the inline configuration menu."""
    from ..config_menu import run_config_menu

    result = run_config_menu()
    if result.saved:
        n = len(result.changes) + (1 if result.api_key_changed else 0)
        typer.echo(f"Saved {n} change(s).")
        return
    typer.echo("Cancelled — no changes saved.")


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Key to set."),
    value: str = typer.Argument(..., help="Value to set."),
) -> None:
    console = _console()
    cfg = _patchable("load_config", load_config)()
    try:
        ensure_subscription_menu_managed_key(cfg, key)
        cfg = set_config_value(cfg, key, value)
        save_config(cfg)
    except ConfigError as e:
        console.print(f"[red]Config error:[/red] {e}")
        raise typer.Exit(code=2) from e
    console.print(f"Saved {key} in {config_path()}")


@config_app.command("set-api-key")
def config_set_api_key(
    env_var: str | None = typer.Option(
        None,
        "--env-var",
        help="Copy the API key from this environment variable.",
    ),
    stdin: bool = typer.Option(
        False,
        "--stdin",
        help="Read the API key from standard input.",
    ),
) -> None:
    console = _console()
    if env_var and stdin:
        console.print("[red]Config error:[/red] Use either --env-var or --stdin, not both.")
        raise typer.Exit(code=2)

    if env_var:
        env_name = env_var.strip()
        if not env_name:
            console.print("[red]Config error:[/red] --env-var must be non-empty.")
            raise typer.Exit(code=2)
        key = str(env_get(env_name) or "").strip()
        if not key:
            console.print(f"[red]Config error:[/red] Environment variable {env_name} is not set.")
            raise typer.Exit(code=2)
    elif stdin:
        key = sys.stdin.read().strip()
        if not key:
            console.print("[red]Config error:[/red] API key is empty.")
            raise typer.Exit(code=2)
    else:
        key = typer.prompt("API key", hide_input=True).strip()
        if not key:
            console.print("[red]Config error:[/red] API key is empty.")
            raise typer.Exit(code=2)

    try:
        save_persisted_api_key(key)
    except ConfigError as e:
        console.print(f"[red]Config error:[/red] {e}")
        raise typer.Exit(code=2) from e
    console.print(f"Saved API key in {credentials_path()}")


@config_app.command("clear-api-key")
def config_clear_api_key() -> None:
    console = _console()
    removed = clear_persisted_api_key()
    if removed:
        console.print(f"Removed persisted API key from {credentials_path()}")
        return
    console.print(f"No persisted API key found at {credentials_path()}")
