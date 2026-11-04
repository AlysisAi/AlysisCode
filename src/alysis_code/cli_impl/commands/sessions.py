from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import typer

from ...config import load_config
from ...session_metrics import score_session_log
from ...session_store import (
    filter_sessions_to_local_owner,
    list_sessions,
    read_session_events,
    resolve_sessions_dir,
)
from ...usage_tracker import aggregate_usage_from_session_logs
from . import _patchable
from ._shared import _console, _Table
from .chat_messaging import messaging_app


def _cli_module() -> Any:
    module = sys.modules.get("alysis_code.cli")
    if module is not None:
        return module
    from ... import cli

    return cli


sessions_app = typer.Typer(add_completion=False, help="Session log commands.")

sessions_app.add_typer(messaging_app, name="messaging")


def _usage_source_pair(row: dict[str, Any]) -> str:
    provider = int(row.get("api_usage_calls") or 0)
    fallback = int(row.get("estimate_usage_calls") or 0)
    return f"{provider}/{fallback}"


def _print_hidden_owner_notice(console: Any, hidden_count: int) -> None:
    """Say how many sessions the owner filter removed.

    Silence here is what made "my session is gone" indistinguishable from "your
    session is filtered": an empty list looks the same either way, so the count is
    printed whenever anything was dropped -- including when nothing is left.
    """
    if hidden_count <= 0:
        return
    suffix = "" if hidden_count == 1 else "s"
    console.print(
        f"[dim]{hidden_count} session{suffix} from other accounts hidden. "
        "Use --all-owners to include.[/dim]"
    )


@sessions_app.command("list")
def sessions_list(
    all_owners: bool = typer.Option(
        False,
        "--all-owners",
        "--all",
        help="Include sessions recorded by other accounts (hidden by default).",
    ),
) -> None:
    console = _console()
    cfg = _patchable("load_config", load_config)()
    sessions_dir = resolve_sessions_dir(cfg)
    all_infos = _patchable("list_sessions", list_sessions)(sessions_dir)
    infos = all_infos if all_owners else filter_sessions_to_local_owner(all_infos)
    hidden_count = len(all_infos) - len(infos)

    table = _Table(title=f"Sessions ({sessions_dir})")
    table.add_column("session_id")
    table.add_column("path")
    for info in infos[:200]:
        table.add_row(info.session_id, os.fspath(info.path))
    console.print(table)
    _print_hidden_owner_notice(console, hidden_count)


@sessions_app.command("show")
def sessions_show(
    session_id: str = typer.Argument(..., help="Session id (filename stem)."),
) -> None:
    console = _console()
    cfg = _patchable("load_config", load_config)()
    sessions_dir = resolve_sessions_dir(cfg)
    path = sessions_dir / f"{session_id}.jsonl"
    if not path.exists():
        console.print(f"[red]Not found:[/red] {path}")
        raise typer.Exit(code=2)
    for ev in read_session_events(path):
        console.print_json(json.dumps(ev))


@sessions_app.command("usage")
def sessions_usage(
    session_id: str = typer.Argument(..., help="Session id (filename stem)."),
) -> None:
    console = _console()
    cfg = _patchable("load_config", load_config)()
    sessions_dir = resolve_sessions_dir(cfg)
    path = sessions_dir / f"{session_id}.jsonl"
    if not path.exists():
        console.print(f"[red]Not found:[/red] {path}")
        raise typer.Exit(code=2)
    summary = _patchable("aggregate_usage_from_session_logs", aggregate_usage_from_session_logs)(
        [path]
    )
    rows = summary.by_model_rows()
    if not rows:
        console.print("No llm_usage events found in this session log.")
        return
    table = _Table(title=f"Session Usage ({session_id})")
    table.add_column("model")
    table.add_column("prompt_tokens", justify="right")
    table.add_column("completion_tokens", justify="right")
    table.add_column("total_tokens", justify="right")
    table.add_column("cost_usd", justify="right")
    table.add_column("unknown_pricing", justify="right")
    table.add_column("usage(provider/fallback)", justify="right")
    for row in rows:
        unknown_count = int(row.get("unknown_cost_count") or 0)
        cost_display = _cli_module()._format_cost_with_unknown(
            known_cost=_cli_module()._known_cost_value(row),
            unknown_calls=unknown_count,
            style="table",
        )
        table.add_row(
            str(row.get("model") or "-"),
            str(int(row.get("prompt_tokens") or 0)),
            str(int(row.get("completion_tokens") or 0)),
            str(int(row.get("total_tokens") or 0)),
            cost_display,
            str(unknown_count),
            _usage_source_pair(row),
        )
    totals = summary.totals()
    total_cost = _cli_module()._format_cost_with_unknown(
        known_cost=_cli_module()._known_cost_value(totals),
        unknown_calls=int(totals.get("unknown_cost_calls") or 0),
        style="table",
    )
    table.add_row(
        "TOTAL",
        str(int(totals.get("prompt_tokens") or 0)),
        str(int(totals.get("completion_tokens") or 0)),
        str(int(totals.get("total_tokens") or 0)),
        total_cost,
        str(int(totals.get("unknown_cost_calls") or 0)),
        _usage_source_pair(totals),
    )
    console.print(table)


@sessions_app.command("score")
def sessions_score(
    session_id: str | None = typer.Argument(
        None,
        help="Session id (filename stem). If omitted, scores latest session(s).",
    ),
    latest: int = typer.Option(
        0,
        "--latest",
        min=0,
        help="Score latest N sessions (ignored when session_id is provided).",
    ),
    all_owners: bool = typer.Option(
        False,
        "--all-owners",
        help="Include sessions recorded by other accounts when picking the latest ones.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    console = _console()
    cfg = _patchable("load_config", load_config)()
    sessions_dir = resolve_sessions_dir(cfg)

    if session_id and latest > 0:
        console.print("[red]Error:[/red] Use either session_id or --latest, not both.")
        raise typer.Exit(code=2)

    target_paths: list[Path] = []
    if session_id:
        path = sessions_dir / f"{session_id}.jsonl"
        if not path.exists():
            console.print(f"[red]Not found:[/red] {path}")
            raise typer.Exit(code=2)
        target_paths = [path]
    else:
        all_infos = _patchable("list_sessions", list_sessions)(sessions_dir)
        infos = all_infos if all_owners else filter_sessions_to_local_owner(all_infos)
        hidden_count = len(all_infos) - len(infos)
        if not infos:
            console.print(f"No sessions owned by this account found in {sessions_dir}")
            _print_hidden_owner_notice(console, hidden_count)
            return
        _print_hidden_owner_notice(console, hidden_count)
        count = latest if latest > 0 else 1
        target_paths = [info.path for info in infos[:count]]

    scores = [_patchable("score_session_log", score_session_log)(path) for path in target_paths]

    if json_output:
        payload: Any = scores[0] if len(scores) == 1 else scores
        console.print_json(json.dumps(payload))
        return

    table = _Table(title=f"Session Score ({sessions_dir})")
    table.add_column("session_id")
    table.add_column("tools", justify="right")
    table.add_column("errors", justify="right")
    table.add_column("writes", justify="right")
    table.add_column("blocked_writes", justify="right")
    table.add_column("read_before_write")
    table.add_column("shell_runs", justify="right")
    table.add_column("test_runs", justify="right")
    table.add_column("llm_calls", justify="right")
    table.add_column("total_tokens", justify="right")

    for score in scores:
        rbw = score.get("read_before_first_write")
        if isinstance(rbw, bool):
            rbw_text = "yes" if rbw else "no"
        else:
            rbw_text = "n/a"
        table.add_row(
            str(score.get("session_id") or "-"),
            str(int(score.get("tool_calls") or 0)),
            str(int(score.get("tool_errors") or 0)),
            str(int(score.get("write_calls") or 0)),
            str(int(score.get("blocked_write_errors") or 0)),
            rbw_text,
            str(int(score.get("shell_runs") or 0)),
            str(int(score.get("test_shell_runs") or 0)),
            str(int(score.get("llm_usage_events") or 0)),
            str(int(score.get("total_tokens") or 0)),
        )
    console.print(table)

    if len(scores) == 1:
        score = scores[0]
        commands = score.get("test_shell_commands") or []
        if commands:
            console.print("Test commands: " + "; ".join(str(cmd) for cmd in commands))
        repeated = score.get("repeated_tool_errors") or []
        for item in repeated:
            console.print(
                f"Repeated tool error (x{int(item.get('count') or 0)}): {item.get('error') or ''}"
            )
