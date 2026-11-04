from __future__ import annotations

import json

import typer

from ...chat_messaging import ChatMessageStore, ChatMessagingError
from ...config import load_config
from ...session_store import (
    filter_sessions_to_local_owner,
    list_sessions,
    resolve_sessions_dir,
)

messaging_app = typer.Typer(
    add_completion=False, help="Authorize and inspect local chat-to-chat messaging."
)


def _store():
    cfg = load_config()
    sessions_dir = resolve_sessions_dir(cfg)
    store = ChatMessageStore(sessions_dir)
    with store.connect() as db:
        known = {row[0] for row in db.execute("SELECT id FROM chats")}
    for info in filter_sessions_to_local_owner(list_sessions(sessions_dir)):
        workspace = info.workspace_root or info.git_root
        identity = store.identity(info.session_id)
        if workspace and identity not in known:
            from pathlib import Path

            store.register(identity, Path(workspace))
            known.add(identity)
    return store, sessions_dir


@messaging_app.command("list")
def list_chats():
    """List local chat IDs and workspace labels without reading their content."""
    store, _ = _store()
    typer.echo(json.dumps(store.list_chats(), ensure_ascii=False, indent=2))


@messaging_app.command("link")
def link_chats(a: str, b: str):
    """Authorize both chats to exchange messages, including across projects."""
    store, _ = _store()
    try:
        store.link(store.identity(a), store.identity(b))
    except ChatMessagingError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo("Chats linked. Each chat keeps its own context and permissions.")


@messaging_app.command("unlink")
def unlink_chats(a: str, b: str):
    """Revoke communication and cancel queued exchanges between these chats."""
    store, _ = _store()
    try:
        store.link(store.identity(a), store.identity(b), enabled=False)
    except ChatMessagingError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo("Link revoked; running peer turns are signalled to stop.")


@messaging_app.command("status")
def message_status(chat: str, message: str):
    """Inspect a message's actual delivery outcome and replies."""
    store, _ = _store()
    try:
        result = store.status(store.identity(chat), message)
    except ChatMessagingError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@messaging_app.command("close")
def close_exchange(chat: str, exchange: str):
    """Stop one exchange without revoking the chat link."""
    store, _ = _store()
    try:
        store.close_exchange(store.identity(chat), exchange)
    except ChatMessagingError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo("Exchange closed.")


@messaging_app.command("serve")
def serve():
    """Wake saved linked chats when messages arrive, independently of the app.

    Uses each recipient's saved provider profile. This may consume model usage.
    Stop this process with Ctrl+C; queued messages remain durable.
    """
    from ...chat_messaging_service import serve_chats

    cfg = load_config()
    if not cfg.chat_messaging_enabled:
        raise typer.BadParameter("Enable chat_messaging_enabled in engine config first.")
    typer.echo("Engine chat receiver running. Ctrl+C stops receiving; saved messages are retained.")
    try:
        serve_chats(resolve_sessions_dir(cfg))
    except KeyboardInterrupt:
        pass
