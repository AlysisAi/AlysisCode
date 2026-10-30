"""Memory management without requiring a model, credentials or a workspace."""

from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any

import typer

from ...local_memory import MemoryStore, validate_scope

memory_app = typer.Typer(help="Manage local general memory shared with the desktop app.")


def memory_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Memory request must be an object.")
    allowed = {
        "action",
        "project_id",
        "offset",
        "id",
        "revision",
        "content",
        "this_computer",
        "enabled",
        "include_general",
    }
    if set(value) - allowed:
        raise ValueError("Unsupported memory request field.")
    store = MemoryStore()
    project_id = validate_scope({"project_id": value.get("project_id")})["project_id"]
    action = value.get("action")
    if action == "read":
        offset = value.get("offset", 0)
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer.")
        return store.snapshot(project_id, offset=offset)
    if action == "export":
        return store.export()
    result = store.change(value, project_id=project_id)
    return {**result, "snapshot": store.snapshot(project_id)}


def emit(value: dict[str, Any]) -> None:
    try:
        typer.echo(json.dumps(memory_request(value), ensure_ascii=False))
    except (ValueError, OSError, sqlite3.Error) as exc:
        typer.echo(json.dumps({"error": str(exc)}, ensure_ascii=False), err=True)
        raise typer.Exit(1) from exc


@memory_app.command("request", hidden=True)
def request() -> None:
    """Desktop transport. Sensitive content is sent on stdin, never argv."""
    try:
        raw = sys.stdin.read(256_001)
        if len(raw) > 256_000:
            raise ValueError("Memory request is too large.")
        value = json.loads(raw)
    except ValueError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(1) from exc
    emit(value)


@memory_app.command("list")
def list_memories(offset: int = typer.Option(0, min=0)) -> None:
    """Show current entries and revisions as JSON; paginate with --offset."""
    emit({"action": "read", "offset": offset})


@memory_app.command()
def enable() -> None:
    """Enable automatic reading and learning in ordinary app and CLI chats."""
    emit({"action": "settings", "enabled": True})


@memory_app.command()
def disable() -> None:
    """Stop reading and learning. Keep saved memories for later."""
    emit({"action": "settings", "enabled": False})


@memory_app.command()
def add(this_computer: bool = False) -> None:
    """Read a new memory from stdin (avoids shell-history disclosure)."""
    emit({"action": "save", "content": sys.stdin.read(4001), "this_computer": this_computer})


@memory_app.command()
def edit(entry_id: str, revision: int, this_computer: bool = False) -> None:
    """Replace an entry with text from stdin; use its current revision."""
    emit(
        {
            "action": "save",
            "id": entry_id,
            "revision": revision,
            "content": sys.stdin.read(4001),
            "this_computer": this_computer,
        }
    )


@memory_app.command()
def delete(entry_id: str, revision: int) -> None:
    """Delete an entry at its current revision, preserving only a deletion receipt."""
    emit({"action": "delete", "id": entry_id, "revision": revision})


@memory_app.command("export")
def export_memories() -> None:
    """Export portable general records as JSON to stdout. No sync is performed."""
    emit({"action": "export"})
