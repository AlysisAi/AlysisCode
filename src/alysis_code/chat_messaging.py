"""Local, durable communication between independently owned agent sessions.

Only the host CLI can grant links. Model tools operate on a bound endpoint and
cannot choose their sender, grant access, or read another chat's transcript.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .logging_redaction import redact_log_text
from .session_store import local_session_owner

MAX_MESSAGE_CHARS = 16000
MAX_EXCHANGE_MESSAGES = 32


class ChatMessagingError(ValueError):
    """A safe, user-visible messaging refusal."""


class ChatAlreadyOwned(ChatMessagingError):
    """An existing live engine retains execution ownership."""


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ChatMessagingError("Invalid chat or message identifier.")
    if any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in value):
        raise ChatMessagingError("Invalid chat or message identifier.")
    return value


class ChatMessageStore:
    def __init__(self, sessions_dir: Path):
        self.directory = sessions_dir / ".chat-messaging"
        if self.directory.is_symlink():
            raise ChatMessagingError("Messaging storage must not be a symbolic link.")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / "mailbox.sqlite3"
        if self.path.is_symlink():
            raise ChatMessagingError("Messaging database must not be a symbolic link.")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, workspace TEXT NOT NULL,
                    title TEXT NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS links (
                    a TEXT NOT NULL, b TEXT NOT NULL, enabled INTEGER NOT NULL,
                    PRIMARY KEY(a,b)
                );
                CREATE TABLE IF NOT EXISTS exchanges (
                    id TEXT PRIMARY KEY, a TEXT NOT NULL, b TEXT NOT NULL,
                    closed INTEGER NOT NULL DEFAULT 0, messages INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                    sender TEXT NOT NULL, recipient TEXT NOT NULL, exchange_id TEXT NOT NULL,
                    reply_to TEXT, content TEXT NOT NULL, request_reply INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                    error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(sender,idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS inbox ON messages(recipient,state,sequence);
                CREATE TABLE IF NOT EXISTS aliases (log_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL);
            """)
        os.chmod(self.path, 0o600)

    def identity(self, log_id: str) -> str:
        with self.connect() as db:
            row = db.execute(
                "SELECT chat_id FROM aliases WHERE log_id=?", (_identifier(log_id),)
            ).fetchone()
            return row[0] if row else log_id

    def alias(self, log_id: str, chat_id: str) -> None:
        with self.connect() as db:
            self._chat(db, chat_id)
            db.execute(
                "INSERT INTO aliases VALUES(?,?) ON CONFLICT(log_id) DO UPDATE SET chat_id=excluded.chat_id",
                (_identifier(log_id), chat_id),
            )

    def abort_pending(self, recipient: str, message_id: str, error: str) -> None:
        with self.connect() as db:
            self._chat(db, recipient)
            db.execute(
                "UPDATE messages SET state='failed',error=?,updated=? WHERE id=? AND recipient=? AND state='queued'",
                (error, time.time(), message_id, recipient),
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def register(self, session_id: str, workspace: Path, title: str = "") -> None:
        owner = local_session_owner()
        if not owner:
            raise ChatMessagingError("Cannot establish the local chat owner.")
        with self.connect() as db:
            row = db.execute(
                "SELECT owner FROM chats WHERE id=?", (_identifier(session_id),)
            ).fetchone()
            if row and row[0] != owner:
                raise ChatMessagingError("Chat belongs to a different local owner.")
            db.execute(
                "INSERT INTO chats VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "workspace=excluded.workspace, title=CASE WHEN excluded.title='' THEN chats.title "
                "ELSE excluded.title END,updated=excluded.updated",
                (session_id, owner, str(workspace.resolve()), title[:200], time.time()),
            )

    def _chat(self, db, session_id: str):
        row = db.execute("SELECT * FROM chats WHERE id=?", (_identifier(session_id),)).fetchone()
        if not row or row["owner"] != local_session_owner():
            raise ChatMessagingError("Chat is unavailable for this local owner.")
        return row

    def link(self, a: str, b: str, *, enabled: bool = True) -> None:
        if a == b:
            raise ChatMessagingError("Choose two different chats.")
        with self.connect() as db:
            self._chat(db, a)
            self._chat(db, b)
            a, b = sorted((a, b))
            db.execute(
                "INSERT INTO links VALUES(?,?,?) ON CONFLICT(a,b) DO UPDATE SET enabled=excluded.enabled",
                (a, b, int(enabled)),
            )
            if not enabled:
                db.execute("UPDATE exchanges SET closed=1 WHERE a=? AND b=?", (a, b))
                db.execute(
                    "UPDATE messages SET state='cancelled',error='link_revoked',updated=? "
                    "WHERE state='queued' AND ((sender=? AND recipient=?) OR (sender=? AND recipient=?))",
                    (time.time(), a, b, b, a),
                )

    def list_chats(self, source: str | None = None) -> list[dict]:
        with self.connect() as db:
            if source is not None:
                own = self._chat(db, source)
                rows = db.execute(
                    "SELECT c.* FROM chats c WHERE c.owner=? AND c.id<>? AND NOT EXISTS "
                    "(SELECT 1 FROM aliases x WHERE x.log_id=c.id AND x.chat_id<>c.id) AND "
                    "(c.workspace=? OR EXISTS(SELECT 1 FROM links l WHERE l.enabled=1 AND "
                    "((l.a=? AND l.b=c.id) OR (l.b=? AND l.a=c.id)))) ORDER BY c.updated DESC LIMIT 100",
                    (own["owner"], source, own["workspace"], source, source),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM chats WHERE owner=? AND NOT EXISTS "
                    "(SELECT 1 FROM aliases x WHERE x.log_id=chats.id AND x.chat_id<>chats.id) ORDER BY updated DESC LIMIT 100",
                    (local_session_owner(),),
                ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item.pop("owner")
                item["linked"] = source is not None and self._linked(db, source, row["id"])
                result.append(item)
            return result

    @staticmethod
    def _linked(db, a: str, b: str) -> bool:
        a, b = sorted((a, b))
        return bool(
            db.execute("SELECT 1 FROM links WHERE a=? AND b=? AND enabled=1", (a, b)).fetchone()
        )

    def send(
        self,
        source: str,
        recipient: str,
        content: str,
        *,
        idempotency_key: str,
        reply_to: str | None = None,
        request_reply: bool = True,
    ) -> dict:
        source, recipient = self.identity(source), self.identity(recipient)
        _identifier(idempotency_key)
        if not isinstance(content, str) or not content.strip() or len(content) > MAX_MESSAGE_CHARS:
            raise ChatMessagingError("Message must contain 1–16000 characters.")
        if type(request_reply) is not bool:
            raise ChatMessagingError("request_reply must be a boolean.")
        content = redact_log_text(content)
        with self.connect() as db:
            self._chat(db, source)
            self._chat(db, recipient)
            prior = db.execute(
                "SELECT * FROM messages WHERE sender=? AND idempotency_key=?",
                (source, idempotency_key),
            ).fetchone()
            if prior:
                if (
                    prior["recipient"],
                    prior["content"],
                    prior["reply_to"],
                    bool(prior["request_reply"]),
                ) != (recipient, content, reply_to, request_reply):
                    raise ChatMessagingError(
                        "Idempotency key already belongs to a different message."
                    )
                return dict(prior)
            if not self._linked(db, source, recipient):
                raise ChatMessagingError(
                    "The user must link these chats before they can communicate."
                )
            outstanding = db.execute(
                "SELECT COUNT(*) FROM messages WHERE recipient=? AND state IN ('queued','processing')",
                (recipient,),
            ).fetchone()[0]
            if outstanding >= 100:
                raise ChatMessagingError(
                    "Recipient mailbox has reached its outstanding-message allowance."
                )
            a, b = sorted((source, recipient))
            if reply_to:
                parent = db.execute(
                    "SELECT * FROM messages WHERE id=?", (_identifier(reply_to),)
                ).fetchone()
                if not parent or parent["sender"] != recipient or parent["recipient"] != source:
                    raise ChatMessagingError(
                        "Reply must reference a message received from this chat."
                    )
                exchange_id = parent["exchange_id"]
            else:
                exchange_id = uuid.uuid4().hex
                db.execute("INSERT INTO exchanges(id,a,b) VALUES(?,?,?)", (exchange_id, a, b))
            exchange = db.execute("SELECT * FROM exchanges WHERE id=?", (exchange_id,)).fetchone()
            if exchange["closed"] or exchange["messages"] >= MAX_EXCHANGE_MESSAGES:
                raise ChatMessagingError("Exchange is closed or has reached its message allowance.")
            message_id, now = uuid.uuid4().hex, time.time()
            db.execute(
                "INSERT INTO messages(id,sender,recipient,exchange_id,reply_to,content,request_reply,idempotency_key,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    message_id,
                    source,
                    recipient,
                    exchange_id,
                    reply_to,
                    content,
                    int(request_reply),
                    idempotency_key,
                    now,
                    now,
                ),
            )
            db.execute("UPDATE exchanges SET messages=messages+1 WHERE id=?", (exchange_id,))
            return dict(db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())

    def inbox(self, session_id: str, *, after: int = 0, pending: bool = False) -> list[dict]:
        if type(after) is not int or after < 0:
            raise ChatMessagingError("Cursor must be a nonnegative integer.")
        with self.connect() as db:
            self._chat(db, session_id)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM messages WHERE recipient=? AND sequence>? "
                    + ("AND state='queued' " if pending else "")
                    + "ORDER BY sequence LIMIT 50",
                    (session_id, after),
                )
            ]

    def message_states(self, session_id: str) -> list[dict]:
        """Owner-visible delivery receipts, without copying message content."""
        with self.connect() as db:
            self._chat(db, session_id)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,state,error,updated FROM messages WHERE sender=? OR recipient=? ORDER BY sequence",
                    (session_id, session_id),
                )
            ]

    def claim(self, session_id: str, message_id: str) -> dict | None:
        with self.connect() as db:
            self._chat(db, session_id)
            row = db.execute(
                "SELECT * FROM messages WHERE id=? AND recipient=? AND state='queued'",
                (_identifier(message_id), session_id),
            ).fetchone()
            if row is None:
                return None
            if not self._linked(db, row["sender"], session_id):
                db.execute(
                    "UPDATE messages SET state='cancelled',error='link_revoked' WHERE id=?",
                    (message_id,),
                )
                return None
            now = time.time()
            db.execute(
                "UPDATE messages SET state='processing',updated=? WHERE id=?",
                (now, message_id),
            )
            return {**dict(row), "state": "processing", "updated": now}

    def settle(
        self, session_id: str, message_id: str, state: str, error: str | None = None
    ) -> None:
        if state not in {"delivered", "failed", "cancelled"}:
            raise ChatMessagingError("Invalid message outcome.")
        with self.connect() as db:
            self._chat(db, session_id)
            db.execute(
                "UPDATE messages SET state=?,error=?,updated=? WHERE id=? AND recipient=? AND state='processing'",
                (state, error, time.time(), message_id, session_id),
            )

    def recover(self, session_id: str) -> None:
        # Called only after acquiring exclusive session ownership. A started
        # peer turn may have performed actions: never automatically repeat it.
        with self.connect() as db:
            self._chat(db, session_id)
            db.execute(
                "UPDATE messages SET state='failed',error='interrupted_indeterminate',updated=? WHERE recipient=? AND state='processing'",
                (time.time(), session_id),
            )

    def status(self, session_id: str, message_id: str) -> dict:
        with self.connect() as db:
            self._chat(db, session_id)
            row = db.execute(
                "SELECT * FROM messages WHERE id=? AND (sender=? OR recipient=?)",
                (_identifier(message_id), session_id, session_id),
            ).fetchone()
            if row is None:
                raise ChatMessagingError("Message is unavailable to this chat.")
            result = dict(row)
            result["replies"] = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM messages WHERE reply_to=? ORDER BY sequence", (message_id,)
                )
            ]
            return result

    def close_exchange(self, session_id: str, exchange_id: str) -> None:
        with self.connect() as db:
            self._chat(db, session_id)
            if (
                db.execute(
                    "SELECT 1 FROM exchanges WHERE id=? AND (a=? OR b=?)",
                    (_identifier(exchange_id), session_id, session_id),
                ).fetchone()
                is None
            ):
                raise ChatMessagingError("Exchange is unavailable to this chat.")
            db.execute(
                "UPDATE exchanges SET closed=1 WHERE id=? AND (a=? OR b=?)",
                (_identifier(exchange_id), session_id, session_id),
            )
            db.execute(
                "UPDATE messages SET state='cancelled',error='exchange_closed',updated=? WHERE exchange_id=? AND state='queued' AND EXISTS(SELECT 1 FROM exchanges WHERE id=? AND closed=1 AND (a=? OR b=?))",
                (time.time(), exchange_id, exchange_id, session_id, session_id),
            )
