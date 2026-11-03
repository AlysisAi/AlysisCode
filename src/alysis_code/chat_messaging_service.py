"""Engine-owned activation of saved, linked chats without a desktop process."""

from __future__ import annotations

import threading
from pathlib import Path

from .chat_messaging import ChatAlreadyOwned, ChatMessageStore, ChatMessagingError
from .chat_messaging_runtime import claim_chat_owner
from .config import load_config
from .profiles import get_profile, resolve_effective_base_url, set_active_profile
from .session_store import filter_sessions_to_local_owner, list_sessions, read_session_events


def restore_chat(session_id: str, sessions_dir: Path, *, create_session_fn=None):
    """Restore the recipient's own profile and history, or refuse before a model call."""
    store = ChatMessageStore(sessions_dir)
    handle = claim_chat_owner(store, session_id)
    try:
        return _restore_owned_chat(session_id, sessions_dir, handle, create_session_fn)
    except BaseException:
        handle.close()
        raise


def _restore_owned_chat(session_id, sessions_dir, handle, create_session_fn):
    from .agent.session import create_session
    from .cli_impl.commands.chat_resume_helpers import (
        _load_chat_resume_messages,
        _restore_chat_resume_evidence,
    )
    from .llm.metadata import endpoint_descriptor
    from .runtime_kind import RuntimeKind

    store = ChatMessageStore(sessions_dir)
    with store.connect() as db:
        alias = db.execute(
            "SELECT log_id FROM aliases WHERE chat_id=? ORDER BY rowid DESC LIMIT 1", (session_id,)
        ).fetchone()
    log_id = alias[0] if alias else session_id
    infos = {
        info.session_id: info
        for info in filter_sessions_to_local_owner(list_sessions(sessions_dir))
    }
    info = infos.get(log_id)
    if info is None:
        raise ChatMessagingError("Recipient session log is unavailable to this local owner.")
    saved, context = None, {}
    for event in read_session_events(info.path):
        if event.get("type") == "session_start":
            saved = event.get("payload", {})
        elif event.get("type") == "chat_messaging_context":
            context = event.get("payload", {})
    if not saved:
        raise ChatMessagingError("Recipient has no saved engine configuration.")
    if (
        saved.get("sensitive_media")
        or saved.get("one_shot_execution")
        or saved.get("subagent_depth", 0)
    ):
        raise ChatMessagingError("This saved session does not accept peer-chat activation.")
    cfg = load_config().model_copy(deep=True)
    profile_name = saved.get("profile_name")
    profile = get_profile(cfg, profile_name) if profile_name else None
    if profile is None:
        raise ChatMessagingError(
            "Recipient's saved provider profile is unavailable; resume it explicitly."
        )
    set_active_profile(cfg, profile_name)
    if endpoint_descriptor(resolve_effective_base_url(cfg=cfg)) != saved.get("base_url_descriptor"):
        raise ChatMessagingError("Recipient's provider endpoint changed; resume it explicitly.")
    for key in (
        "model",
        "temperature",
        "coding_temperature",
        "llm_reasoning_effort",
        "llm_enable_thinking",
    ):
        if key in saved:
            setattr(cfg, key, saved[key])
    cfg.chat_messaging_enabled = True
    cfg.stream = False
    mode = context.get("mode", saved.get("mode"))
    if mode not in {"readonly", "review", "auto", "fullaccess"}:
        raise ChatMessagingError("Recipient has no valid saved permission mode.")
    root = Path(saved.get("workspace_root") or saved.get("root") or "")
    if not root.is_absolute() or not root.is_dir():
        raise ChatMessagingError("Recipient's workspace is unavailable.")
    history = _load_chat_resume_messages(info.path)
    session = None
    try:
        session = (create_session_fn or create_session)(
            cfg=cfg,
            root=root,
            mode=mode,
            yes=False,
            max_steps=12,
            no_log=False,
            non_interactive=True,
            runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
            session_log_dir_override=sessions_dir,
            session_id_override=log_id,
            deny_write_prefixes=context.get(
                "deny_write_prefixes", saved.get("deny_write_prefixes")
            ),
            allow_write_globs=context.get("allow_write_globs", saved.get("allow_write_globs")),
            persona_allow_write_globs=context.get("persona_allow_write_globs"),
            chat_messaging_owner_handle=handle,
            chat_messaging_defer_start=True,
        )
        session.messages.extend(history)
        session.memory_scope = context.get("memory_scope", {"project_id": None, "enabled": False})
        session.project_folders = context.get("project_folders", [])
        _restore_chat_resume_evidence(session, path=info.path, source_session_id=log_id)
        session.chat_endpoint.start()
        return session
    except BaseException:
        if session is not None:
            session.close()
        raise


def serve_chats(sessions_dir: Path, *, stop: threading.Event | None = None) -> None:
    """Activate only queued, authorized recipients. Existing owners keep their chats."""
    store = ChatMessageStore(sessions_dir)
    stop = stop or threading.Event()
    sessions = {}
    try:
        while not stop.wait(0.5):
            # Release drained recipients so a terminal or desktop can acquire
            # them again. The receiver does not retain idle chat ownership.
            for recipient, session in list(sessions.items()):
                endpoint = session.chat_endpoint
                if not endpoint.turn_lock.acquire(blocking=False):
                    continue
                try:
                    idle = endpoint.current_message is None and not store.inbox(
                        recipient, pending=True
                    )
                finally:
                    endpoint.turn_lock.release()
                if idle:
                    session.close()
                    del sessions[recipient]
            with store.connect() as db:
                recipients = [
                    row[0]
                    for row in db.execute(
                        "SELECT DISTINCT recipient FROM messages WHERE state='queued'"
                    )
                ]
            for recipient in recipients:
                if recipient in sessions:
                    continue
                try:
                    sessions[recipient] = restore_chat(recipient, sessions_dir)
                except ChatAlreadyOwned:
                    # A live endpoint is normal: its existing owner drains the inbox.
                    continue
                except ChatMessagingError as exc:
                    for message in store.inbox(recipient, pending=True):
                        store.abort_pending(
                            recipient, message["id"], "recipient_unavailable: " + str(exc)
                        )
                except Exception:
                    for message in store.inbox(recipient, pending=True):
                        store.abort_pending(recipient, message["id"], "recipient_start_failed")
    finally:
        for session in sessions.values():
            session.close()
