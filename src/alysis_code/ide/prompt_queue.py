"""Durable, fenced prompt queue for IDE clients.

The queue intentionally owns no execution threads.  A bridge enqueues a JSON
payload, claims the next item for a session, and completes (or fails) that item
with the returned opaque lease token.  SQLite ``BEGIN IMMEDIATE`` transactions
and a per-session partial unique index prevent two bridge processes from
holding live work for the same session at once.

Prompt payloads are never included in dataclass representations or exception
messages.  Storage uses JSON rather than pickle so reopening a database cannot
execute serialized code.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TypeAlias

from ..branding import canonical_user_data_dir, env_get

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

SCHEMA_VERSION = 2
DEFAULT_LEASE_SECONDS = 120.0
MIN_LEASE_SECONDS = 1.0
MAX_LEASE_SECONDS = 24 * 60 * 60.0
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")

# A lease owner that holds its file (``hold_owner``) is known to be gone once its
# file can be locked: the operating system frees the lock when the process ends,
# however it ends. Files of gone owners older than this are removed.
_OWNER_DIR_NAME = "prompt-queue-owners"
_STALE_OWNER_FILE_SECONDS = 60 * 60.0


def _owner_file_name(owner_id: str) -> str:
    return hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:32] + ".lock"


def _lock_without_waiting(handle: Any) -> bool:
    """Take an exclusive lock on an open file; False when another holder has it."""
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: Any) -> None:
    with suppress(OSError):
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class PromptState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL_STATES = frozenset({PromptState.COMPLETED, PromptState.CANCELLED, PromptState.FAILED})


class PromptQueueError(RuntimeError):
    """Base error with messages safe to surface or log."""


class PromptQueueValidationError(PromptQueueError, ValueError):
    pass


class PromptQueueCapacityError(PromptQueueError):
    pass


class PromptIdempotencyConflict(PromptQueueError):
    pass


class PromptNotFound(PromptQueueError):
    pass


class PromptStateError(PromptQueueError):
    pass


class PromptLeaseLost(PromptQueueError):
    pass


class PromptQueueStorageError(PromptQueueError):
    pass


@dataclass(frozen=True, slots=True)
class PromptQueueConfig:
    max_payload_bytes: int = 1024 * 1024
    max_json_depth: int = 24
    max_json_nodes: int = 50_000
    max_outstanding_per_session: int = 100
    max_list_limit: int = 500
    busy_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        positive_ints = (
            self.max_payload_bytes,
            self.max_json_depth,
            self.max_json_nodes,
            self.max_outstanding_per_session,
            self.max_list_limit,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in positive_ints
        ):
            raise PromptQueueValidationError("Queue limits must be positive integers.")
        if (
            isinstance(self.busy_timeout_seconds, bool)
            or not isinstance(self.busy_timeout_seconds, (int, float))
            or not math.isfinite(self.busy_timeout_seconds)
            or self.busy_timeout_seconds <= 0
            or self.busy_timeout_seconds > 60
        ):
            raise PromptQueueValidationError("Queue busy timeout is outside the allowed range.")


@dataclass(frozen=True, slots=True)
class PromptQueueItem:
    prompt_id: str
    sequence: int
    session_id: str = field(repr=False)
    idempotency_key: str = field(repr=False)
    payload: dict[str, JsonValue] = field(repr=False)
    state: PromptState
    created_at: float
    updated_at: float
    attempts: int
    lease_expires_at: float | None = field(default=None, repr=False)
    execution_started_at: float | None = None
    terminal_at: float | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    item: PromptQueueItem
    created: bool


@dataclass(frozen=True, slots=True)
class PromptLease:
    item: PromptQueueItem
    lease_token: str = field(repr=False)
    owner_id: str = field(repr=False)
    expires_at: float


def default_prompt_queue_path() -> Path:
    """Return the user-data queue path, deliberately outside any workspace."""

    override = str(env_get("ALYSIS_DATA_DIR") or "").strip()
    data_dir = Path(override).expanduser() if override else canonical_user_data_dir()
    return data_dir / "ide" / "prompt-queue.sqlite3"


class DurablePromptQueue:
    """SQLite-backed ordered prompt queue.

    Connections are short-lived and are never shared between threads.  SQLite
    serializes writers across processes; an in-process lock also prevents
    schema setup and writes from needlessly contending in one bridge.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        config: PromptQueueConfig | None = None,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else default_prompt_queue_path()
        self.config = config or PromptQueueConfig()
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._lock = threading.RLock()
        self._held_owners: dict[str, Any] = {}
        self._prepare_storage()

    def hold_owner(self, owner_id: str) -> bool:
        """Show other processes that ``owner_id`` is alive, for as long as this one runs.

        The owner locks a small file next to the database. When its process
        ends, however it ends, the operating system frees the lock, and the
        owner's running prompt can be reclaimed at once instead of after its
        lease (up to ``DEFAULT_LEASE_SECONDS``). Returns whether it is held.
        """

        owner_id = _validate_identifier(owner_id, "owner", max_length=256)
        with self._lock:
            if owner_id in self._held_owners:
                return True
            owners = self.path.parent / _OWNER_DIR_NAME
            try:
                owners.mkdir(parents=True, exist_ok=True)
                handle = open(owners / _owner_file_name(owner_id), "a+b")  # noqa: SIM115
            except OSError:
                return False
            if not _lock_without_waiting(handle):
                handle.close()
                return False
            self._held_owners[owner_id] = handle
        self._remove_stale_owner_files()
        return True

    def release_owner(self, owner_id: str) -> None:
        """Stop holding ``owner_id``; its running prompts become reclaimable.

        The file stays, unlocked, so other processes see the owner is gone; it
        is removed with other old files of gone owners.
        """

        with self._lock:
            handle = self._held_owners.pop(owner_id, None)
        if handle is None:
            return
        _unlock(handle)
        handle.close()

    def _owner_is_gone(self, owner_id: Any) -> bool:
        """True only for an owner that held its file and whose process has ended.

        An owner without a file (an older engine) is never called gone: its
        lease runs out as before.
        """

        if not isinstance(owner_id, str) or not owner_id or owner_id in self._held_owners:
            return False
        path = self.path.parent / _OWNER_DIR_NAME / _owner_file_name(owner_id)
        try:
            handle = open(path, "r+b")  # noqa: SIM115
        except OSError:
            return False
        try:
            if not _lock_without_waiting(handle):
                return False
            _unlock(handle)
            return True
        finally:
            handle.close()

    def _expire_gone_owner_leases(
        self, connection: sqlite3.Connection, *, session_id: str, now: float
    ) -> None:
        rows = connection.execute(
            """
            SELECT sequence, lease_owner FROM prompt_queue_items
            WHERE session_id = ? AND state = 'running' AND lease_expires_at > ?
            """,
            (session_id, now),
        ).fetchall()
        for row in rows:
            if self._owner_is_gone(row["lease_owner"]):
                connection.execute(
                    """
                    UPDATE prompt_queue_items SET lease_expires_at = ?, updated_at = ?
                    WHERE sequence = ? AND state = 'running' AND lease_owner = ?
                    """,
                    (now, now, row["sequence"], row["lease_owner"]),
                )

    def _remove_stale_owner_files(self) -> None:
        owners = self.path.parent / _OWNER_DIR_NAME
        cutoff = time.time() - _STALE_OWNER_FILE_SECONDS
        with suppress(OSError):
            for path in owners.glob("*.lock"):
                with suppress(OSError):
                    if path.stat().st_mtime >= cutoff:
                        continue
                    with open(path, "r+b") as handle:
                        if not _lock_without_waiting(handle):
                            continue
                        _unlock(handle)
                    path.unlink()

    def enqueue(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> EnqueueResult:
        """Append a prompt, or return its prior row for an identical retry.

        Reusing ``(session_id, idempotency_key)`` with a different payload is
        rejected.  No payload fragments appear in that error.
        """

        session_id = _validate_identifier(session_id, "session", max_length=256)
        idempotency_key = _validate_identifier(idempotency_key, "idempotency", max_length=256)
        payload_json, normalized_payload = _serialize_payload(payload, self.config)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

        with self._lock, self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM prompt_queue_items
                WHERE session_id = ? AND idempotency_key = ?
                """,
                (session_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["payload_sha256"] != payload_sha256:
                    raise PromptIdempotencyConflict(
                        "Idempotency key was already used for a different prompt."
                    )
                return EnqueueResult(item=_item_from_row(existing), created=False)

            outstanding = connection.execute(
                """
                SELECT COUNT(*) FROM prompt_queue_items
                WHERE session_id = ? AND state IN ('pending', 'running')
                """,
                (session_id,),
            ).fetchone()[0]
            if outstanding >= self.config.max_outstanding_per_session:
                raise PromptQueueCapacityError("Session prompt queue is full.")

            prompt_id = _validate_identifier(self._id_factory(), "generated prompt", max_length=256)
            now = self._now()
            try:
                connection.execute(
                    """
                    INSERT INTO prompt_queue_items (
                        prompt_id, session_id, idempotency_key,
                        payload_json, payload_sha256, state,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        prompt_id,
                        session_id,
                        idempotency_key,
                        payload_json,
                        payload_sha256,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                # Another process may have inserted the same idempotency key
                # after our first SELECT.  Inspect it without exposing values.
                existing = connection.execute(
                    """
                    SELECT * FROM prompt_queue_items
                    WHERE session_id = ? AND idempotency_key = ?
                    """,
                    (session_id, idempotency_key),
                ).fetchone()
                if existing is None:
                    raise PromptQueueStorageError("Could not persist queued prompt.") from None
                if existing["payload_sha256"] != payload_sha256:
                    raise PromptIdempotencyConflict(
                        "Idempotency key was already used for a different prompt."
                    ) from None
                return EnqueueResult(item=_item_from_row(existing), created=False)

            row = connection.execute(
                "SELECT * FROM prompt_queue_items WHERE prompt_id = ?", (prompt_id,)
            ).fetchone()
            if row is None:  # defensive; insert and lookup share a transaction
                raise PromptQueueStorageError("Could not load queued prompt.")
            item = _item_from_row(row, payload=normalized_payload)
            return EnqueueResult(item=item, created=True)

    def get(self, *, session_id: str, prompt_id: str) -> PromptQueueItem | None:
        session_id = _validate_identifier(session_id, "session", max_length=256)
        prompt_id = _validate_identifier(prompt_id, "prompt", max_length=256)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM prompt_queue_items
                WHERE session_id = ? AND prompt_id = ?
                """,
                (session_id, prompt_id),
            ).fetchone()
        return None if row is None else _item_from_row(row)

    def list(
        self,
        *,
        session_id: str,
        states: Iterable[PromptState | str] | None = None,
        limit: int = 100,
        after_sequence: int = 0,
    ) -> list[PromptQueueItem]:
        session_id = _validate_identifier(session_id, "session", max_length=256)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise PromptQueueValidationError("Queue list limit must be an integer.")
        if limit < 1 or limit > self.config.max_list_limit:
            raise PromptQueueValidationError("Queue list limit is outside the allowed range.")
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int):
            raise PromptQueueValidationError("Queue cursor must be an integer.")
        if after_sequence < 0:
            raise PromptQueueValidationError("Queue cursor cannot be negative.")

        state_values = _normalize_states(states)
        sql = "SELECT * FROM prompt_queue_items WHERE session_id = ? AND sequence > ?"
        params: list[Any] = [session_id, after_sequence]
        if state_values:
            placeholders = ", ".join("?" for _ in state_values)
            sql += f" AND state IN ({placeholders})"
            params.extend(state_values)
        sql += " ORDER BY sequence ASC LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_item_from_row(row) for row in rows]

    def is_reclaimable(self, *, session_id: str, prompt_id: str) -> bool:
        """Return whether a logical session may claim this outstanding prompt."""

        session_id = _validate_identifier(session_id, "session", max_length=256)
        prompt_id = _validate_identifier(prompt_id, "prompt", max_length=256)
        now = self._now()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT state, lease_expires_at, lease_owner FROM prompt_queue_items
                WHERE session_id = ? AND prompt_id = ?
                """,
                (session_id, prompt_id),
            ).fetchone()
        if row is None:
            return False
        return row["state"] == PromptState.PENDING.value or (
            row["state"] == PromptState.RUNNING.value
            and row["lease_expires_at"] is not None
            and (float(row["lease_expires_at"]) <= now or self._owner_is_gone(row["lease_owner"]))
        )

    def claim_next(
        self,
        *,
        session_id: str,
        owner_id: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> PromptLease | None:
        """Atomically claim the oldest eligible prompt for a session.

        A non-expired running item blocks later prompts, preserving session
        order.  An expired running item is reclaimed with a new fencing token
        and an incremented attempt count.
        """

        session_id = _validate_identifier(session_id, "session", max_length=256)
        owner_id = _validate_identifier(owner_id, "owner", max_length=256)
        lease_seconds = _validate_lease_seconds(lease_seconds)
        now = self._now()
        expires_at = now + lease_seconds
        lease_token = uuid.uuid4().hex

        with self._lock, self._transaction() as connection:
            self._expire_gone_owner_leases(connection, session_id=session_id, now=now)
            active = connection.execute(
                """
                SELECT 1 FROM prompt_queue_items
                WHERE session_id = ? AND state = 'running'
                  AND lease_expires_at > ?
                LIMIT 1
                """,
                (session_id, now),
            ).fetchone()
            if active is not None:
                return None

            row = connection.execute(
                """
                SELECT * FROM prompt_queue_items
                WHERE session_id = ?
                  AND (
                    state = 'pending'
                    OR (state = 'running' AND lease_expires_at <= ?)
                  )
                ORDER BY sequence ASC
                LIMIT 1
                """,
                (session_id, now),
            ).fetchone()
            if row is None:
                return None

            connection.execute(
                """
                UPDATE prompt_queue_items
                SET state = 'running', lease_token = ?, lease_owner = ?,
                    lease_session_id = ?, lease_expires_at = ?, attempts = attempts + 1,
                    updated_at = ?, terminal_at = NULL, error_code = NULL
                WHERE sequence = ?
                """,
                (lease_token, owner_id, session_id, expires_at, now, row["sequence"]),
            )
            claimed = connection.execute(
                "SELECT * FROM prompt_queue_items WHERE sequence = ?",
                (row["sequence"],),
            ).fetchone()
            if claimed is None:
                raise PromptQueueStorageError("Could not load claimed prompt.")
            return PromptLease(
                item=_item_from_row(claimed),
                lease_token=lease_token,
                owner_id=owner_id,
                expires_at=expires_at,
            )

    def renew(
        self,
        *,
        session_id: str,
        prompt_id: str,
        lease_token: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> PromptLease:
        session_id, prompt_id, lease_token = _validate_lease_identifiers(
            session_id, prompt_id, lease_token
        )
        lease_seconds = _validate_lease_seconds(lease_seconds)
        now = self._now()
        expires_at = now + lease_seconds
        with self._lock, self._transaction() as connection:
            row = self._require_live_lease(
                connection,
                session_id=session_id,
                prompt_id=prompt_id,
                lease_token=lease_token,
                now=now,
            )
            connection.execute(
                """
                UPDATE prompt_queue_items
                SET lease_expires_at = ?, updated_at = ?
                WHERE sequence = ?
                """,
                (expires_at, now, row["sequence"]),
            )
            renewed = connection.execute(
                "SELECT * FROM prompt_queue_items WHERE sequence = ?", (row["sequence"],)
            ).fetchone()
            assert renewed is not None
            return PromptLease(
                item=_item_from_row(renewed),
                lease_token=lease_token,
                owner_id=renewed["lease_owner"],
                expires_at=expires_at,
            )

    def complete(self, *, session_id: str, prompt_id: str, lease_token: str) -> PromptQueueItem:
        return self._finish(
            session_id=session_id,
            prompt_id=prompt_id,
            lease_token=lease_token,
            state=PromptState.COMPLETED,
            error_code=None,
        )

    def mark_execution_started(
        self, *, session_id: str, prompt_id: str, lease_token: str
    ) -> PromptQueueItem:
        """Durably cross the at-most-once execution boundary for a live lease.

        The marker belongs to the prompt identity rather than a SessionStore.
        It therefore survives old-session to new-session rebinding. An expired
        prompt carrying this marker must be failed as indeterminate instead of
        being executed automatically again.
        """

        session_id, prompt_id, lease_token = _validate_lease_identifiers(
            session_id, prompt_id, lease_token
        )
        now = self._now()
        with self._lock, self._transaction() as connection:
            row = self._require_live_lease(
                connection,
                session_id=session_id,
                prompt_id=prompt_id,
                lease_token=lease_token,
                now=now,
            )
            if row["execution_started_at"] is not None:
                raise PromptStateError("Prompt execution has already started.")
            connection.execute(
                """
                UPDATE prompt_queue_items
                SET execution_started_at = ?, updated_at = ?
                WHERE sequence = ?
                """,
                (now, now, row["sequence"]),
            )
            started = connection.execute(
                "SELECT * FROM prompt_queue_items WHERE sequence = ?", (row["sequence"],)
            ).fetchone()
            assert started is not None
            return _item_from_row(started)

    def fail(
        self,
        *,
        session_id: str,
        prompt_id: str,
        lease_token: str,
        error_code: str = "execution_failed",
    ) -> PromptQueueItem:
        if (
            not isinstance(error_code, str)
            or len(error_code) > 64
            or not _ERROR_CODE_RE.fullmatch(error_code)
        ):
            raise PromptQueueValidationError("Prompt failure code is invalid.")
        return self._finish(
            session_id=session_id,
            prompt_id=prompt_id,
            lease_token=lease_token,
            state=PromptState.FAILED,
            error_code=error_code,
        )

    def release(self, *, session_id: str, prompt_id: str, lease_token: str) -> PromptQueueItem:
        """Return a live claim to pending during a graceful bridge shutdown."""

        session_id, prompt_id, lease_token = _validate_lease_identifiers(
            session_id, prompt_id, lease_token
        )
        now = self._now()
        with self._lock, self._transaction() as connection:
            row = self._require_live_lease(
                connection,
                session_id=session_id,
                prompt_id=prompt_id,
                lease_token=lease_token,
                now=now,
            )
            connection.execute(
                """
                UPDATE prompt_queue_items
                SET state = 'pending', lease_token = NULL, lease_owner = NULL,
                    lease_session_id = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE sequence = ?
                """,
                (now, row["sequence"]),
            )
            released = connection.execute(
                "SELECT * FROM prompt_queue_items WHERE sequence = ?", (row["sequence"],)
            ).fetchone()
            assert released is not None
            return _item_from_row(released)

    def delete_pending(self, *, session_id: str, prompt_id: str) -> PromptQueueItem:
        """Cancel, but retain, a pending prompt for audit and idempotency."""

        session_id = _validate_identifier(session_id, "session", max_length=256)
        prompt_id = _validate_identifier(prompt_id, "prompt", max_length=256)
        now = self._now()
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM prompt_queue_items
                WHERE session_id = ? AND prompt_id = ?
                """,
                (session_id, prompt_id),
            ).fetchone()
            if row is None:
                raise PromptNotFound("Queued prompt was not found.")
            if row["state"] != PromptState.PENDING.value:
                raise PromptStateError("Only pending prompts can be deleted.")
            connection.execute(
                """
                UPDATE prompt_queue_items
                SET state = 'cancelled', updated_at = ?, terminal_at = ?
                WHERE sequence = ?
                """,
                (now, now, row["sequence"]),
            )
            cancelled = connection.execute(
                "SELECT * FROM prompt_queue_items WHERE sequence = ?", (row["sequence"],)
            ).fetchone()
            assert cancelled is not None
            return _item_from_row(cancelled)

    def cancel_pending_for_session(self, *, session_id: str) -> int:
        """Atomically cancel every pending prompt for an explicit session."""

        session_id = _validate_identifier(session_id, "session", max_length=256)
        now = self._now()
        with self._lock, self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE prompt_queue_items
                SET state = 'cancelled', updated_at = ?, terminal_at = ?
                WHERE session_id = ? AND state = 'pending'
                """,
                (now, now, session_id),
            )
            return max(0, int(cursor.rowcount))

    def cancel_claimed(
        self, *, session_id: str, prompt_id: str, lease_token: str
    ) -> PromptQueueItem:
        """Mark work cancelled after its fenced executor has stopped."""

        return self._finish(
            session_id=session_id,
            prompt_id=prompt_id,
            lease_token=lease_token,
            state=PromptState.CANCELLED,
            error_code="cancelled",
        )

    def rebind_recoverable(
        self, *, source_session_id: str, target_session_id: str
    ) -> dict[str, int]:
        """Rebind outstanding work while preserving a live executor's fence.

        ``session_id`` is the logical queue owner shown to the resumed client;
        ``lease_session_id`` remains the session accepted from the executor
        holding the opaque lease. Moving a live row is therefore safe: it blocks
        later target-session prompts, while only the old fenced worker can renew
        or finish it. Expired rows become pending under the new session.
        """

        source = _validate_identifier(source_session_id, "source session", max_length=256)
        target = _validate_identifier(target_session_id, "target session", max_length=256)
        if source == target:
            return {"pending": 0, "recovered": 0, "active": 0}
        now = self._now()
        with self._lock, self._transaction() as connection:
            self._expire_gone_owner_leases(connection, session_id=source, now=now)
            conflict = connection.execute(
                """
                SELECT 1
                FROM prompt_queue_items source
                JOIN prompt_queue_items target
                  ON target.session_id = ?
                 AND target.idempotency_key = source.idempotency_key
                WHERE source.session_id = ?
                  AND source.state IN ('pending', 'running')
                LIMIT 1
                """,
                (target, source),
            ).fetchone()
            if conflict is not None:
                raise PromptIdempotencyConflict(
                    "Recoverable session contains a conflicting queued prompt."
                )
            target_running = connection.execute(
                """
                SELECT 1 FROM prompt_queue_items
                WHERE session_id = ? AND state = 'running'
                LIMIT 1
                """,
                (target,),
            ).fetchone()
            source_running = connection.execute(
                """
                SELECT 1 FROM prompt_queue_items
                WHERE session_id = ? AND state = 'running'
                LIMIT 1
                """,
                (source,),
            ).fetchone()
            if target_running is not None and source_running is not None:
                raise PromptStateError("Target session already has active queued work.")

            pending = connection.execute(
                """
                UPDATE prompt_queue_items
                SET session_id = ?, updated_at = ?
                WHERE session_id = ? AND state = 'pending'
                """,
                (target, now, source),
            ).rowcount
            recovered = connection.execute(
                """
                UPDATE prompt_queue_items
                SET session_id = ?, state = 'pending', lease_token = NULL,
                    lease_owner = NULL, lease_session_id = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE session_id = ? AND state = 'running' AND lease_expires_at <= ?
                """,
                (target, now, source, now),
            ).rowcount
            active = connection.execute(
                """
                UPDATE prompt_queue_items
                SET session_id = ?, updated_at = ?
                WHERE session_id = ? AND state = 'running' AND lease_expires_at > ?
                """,
                (target, now, source, now),
            ).rowcount
            return {
                "pending": max(0, pending),
                "recovered": max(0, recovered),
                "active": max(0, active),
            }

    def _finish(
        self,
        *,
        session_id: str,
        prompt_id: str,
        lease_token: str,
        state: PromptState,
        error_code: str | None,
    ) -> PromptQueueItem:
        session_id, prompt_id, lease_token = _validate_lease_identifiers(
            session_id, prompt_id, lease_token
        )
        now = self._now()
        with self._lock, self._transaction() as connection:
            row = self._require_live_lease(
                connection,
                session_id=session_id,
                prompt_id=prompt_id,
                lease_token=lease_token,
                now=now,
            )
            connection.execute(
                """
                UPDATE prompt_queue_items
                SET state = ?, lease_token = NULL, lease_owner = NULL,
                    lease_session_id = NULL, lease_expires_at = NULL,
                    updated_at = ?, terminal_at = ?,
                    error_code = ?
                WHERE sequence = ?
                """,
                (state.value, now, now, error_code, row["sequence"]),
            )
            finished = connection.execute(
                "SELECT * FROM prompt_queue_items WHERE sequence = ?", (row["sequence"],)
            ).fetchone()
            assert finished is not None
            return _item_from_row(finished)

    def _require_live_lease(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        prompt_id: str,
        lease_token: str,
        now: float,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM prompt_queue_items
            WHERE prompt_id = ?
            """,
            (prompt_id,),
        ).fetchone()
        if row is None:
            raise PromptNotFound("Queued prompt was not found.")
        if (
            session_id not in {row["session_id"], row["lease_session_id"]}
            or row["state"] != PromptState.RUNNING.value
            or row["lease_token"] != lease_token
            or row["lease_expires_at"] is None
            or row["lease_expires_at"] <= now
        ):
            raise PromptLeaseLost("Prompt lease is no longer valid.")
        return row

    def _now(self) -> float:
        try:
            now = float(self._clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise PromptQueueValidationError("Queue clock returned an invalid value.") from exc
        if not math.isfinite(now) or now < 0:
            raise PromptQueueValidationError("Queue clock returned an invalid value.")
        return now

    def _prepare_storage(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _restrict_permissions(self.path.parent, 0o700)
            with self._connection() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in (0, 1, SCHEMA_VERSION):
                    raise PromptQueueStorageError(
                        "Prompt queue database schema is not supported by this version."
                    )
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS prompt_queue_items (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        prompt_id TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (
                            state IN ('pending', 'running', 'completed', 'cancelled', 'failed')
                        ),
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                        lease_token TEXT,
                        lease_owner TEXT,
                        lease_session_id TEXT,
                        lease_expires_at REAL,
                        execution_started_at REAL,
                        terminal_at REAL,
                        error_code TEXT,
                        UNIQUE (session_id, idempotency_key),
                        CHECK (
                            (state = 'running' AND lease_token IS NOT NULL
                                AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
                            OR
                            (state != 'running' AND lease_token IS NULL
                                AND lease_owner IS NULL AND lease_expires_at IS NULL)
                        )
                    );
                    CREATE INDEX IF NOT EXISTS prompt_queue_session_order
                        ON prompt_queue_items (session_id, sequence);
                    CREATE INDEX IF NOT EXISTS prompt_queue_session_state_order
                        ON prompt_queue_items (session_id, state, sequence);
                    CREATE UNIQUE INDEX IF NOT EXISTS prompt_queue_one_running_per_session
                        ON prompt_queue_items (session_id) WHERE state = 'running';
                    """
                )
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(prompt_queue_items)")
                }
                if "lease_session_id" not in columns:
                    connection.execute(
                        "ALTER TABLE prompt_queue_items ADD COLUMN lease_session_id TEXT"
                    )
                if "execution_started_at" not in columns:
                    connection.execute(
                        "ALTER TABLE prompt_queue_items ADD COLUMN execution_started_at REAL"
                    )
                connection.execute(
                    """
                    UPDATE prompt_queue_items
                    SET lease_session_id = session_id
                    WHERE state = 'running' AND lease_session_id IS NULL
                    """
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            _restrict_permissions(self.path, 0o600)
        except PromptQueueError:
            raise
        except (OSError, sqlite3.Error):
            raise PromptQueueStorageError("Could not initialize prompt queue storage.") from None

    @contextmanager
    def _connection(self) -> Any:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self.config.busy_timeout_seconds,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            busy_timeout_ms = int(self.config.busy_timeout_seconds * 1000)
            connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            raise PromptQueueStorageError("Could not open prompt queue storage.") from None
        try:
            yield connection
        finally:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    @contextmanager
    def _transaction(self) -> Any:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except PromptQueueError:
                _rollback_quietly(connection)
                raise
            except sqlite3.Error:
                _rollback_quietly(connection)
                raise PromptQueueStorageError("Prompt queue operation failed.") from None
            except BaseException:
                _rollback_quietly(connection)
                raise


def _serialize_payload(
    payload: Mapping[str, Any], config: PromptQueueConfig
) -> tuple[str, dict[str, JsonValue]]:
    if not isinstance(payload, Mapping):
        raise PromptQueueValidationError("Prompt payload must be a JSON object.")
    normalized = _normalize_json(payload, config=config)
    if not isinstance(normalized, dict):  # Mapping always normalizes to a dict
        raise PromptQueueValidationError("Prompt payload must be a JSON object.")
    try:
        serialized = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        raise PromptQueueValidationError("Prompt payload is not valid JSON.") from None
    if len(serialized.encode("utf-8")) > config.max_payload_bytes:
        raise PromptQueueValidationError("Prompt payload exceeds the queue size limit.")
    return serialized, normalized


def _normalize_json(value: Any, *, config: PromptQueueConfig) -> JsonValue:
    nodes = 0

    def visit(item: Any, depth: int) -> JsonValue:
        nonlocal nodes
        nodes += 1
        if nodes > config.max_json_nodes:
            raise PromptQueueValidationError("Prompt payload has too many JSON values.")
        if depth > config.max_json_depth:
            raise PromptQueueValidationError("Prompt payload is nested too deeply.")
        if item is None or isinstance(item, (bool, str)):
            return item
        if isinstance(item, int):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise PromptQueueValidationError("Prompt payload contains an invalid number.")
            return item
        if isinstance(item, Mapping):
            result: dict[str, JsonValue] = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise PromptQueueValidationError("Prompt payload object keys must be strings.")
                if len(key) > 512 or _contains_control_character(key):
                    raise PromptQueueValidationError("Prompt payload object key is invalid.")
                result[key] = visit(child, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            return [visit(child, depth + 1) for child in item]
        raise PromptQueueValidationError("Prompt payload contains a non-JSON value.")

    return visit(value, 0)


def _item_from_row(
    row: sqlite3.Row, *, payload: dict[str, JsonValue] | None = None
) -> PromptQueueItem:
    if payload is None:
        try:
            decoded = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            raise PromptQueueStorageError("Stored prompt payload is invalid.") from None
        if not isinstance(decoded, dict):
            raise PromptQueueStorageError("Stored prompt payload is invalid.")
        payload = decoded
    return PromptQueueItem(
        prompt_id=row["prompt_id"],
        sequence=int(row["sequence"]),
        session_id=row["session_id"],
        idempotency_key=row["idempotency_key"],
        payload=payload,
        state=PromptState(row["state"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        attempts=int(row["attempts"]),
        lease_expires_at=(
            None if row["lease_expires_at"] is None else float(row["lease_expires_at"])
        ),
        execution_started_at=(
            None if row["execution_started_at"] is None else float(row["execution_started_at"])
        ),
        terminal_at=None if row["terminal_at"] is None else float(row["terminal_at"]),
        error_code=row["error_code"],
    )


def _normalize_states(states: Iterable[PromptState | str] | None) -> list[str]:
    if states is None:
        return []
    if isinstance(states, (str, bytes)):
        raise PromptQueueValidationError("Queue states must be a collection.")
    normalized: list[str] = []
    try:
        for state in states:
            normalized.append(PromptState(state).value)
    except (TypeError, ValueError):
        raise PromptQueueValidationError("Queue state filter is invalid.") from None
    return list(dict.fromkeys(normalized))


def _validate_identifier(value: Any, kind: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise PromptQueueValidationError(f"Prompt {kind} identifier must be a string.")
    if not value or len(value) > max_length or not _IDENTIFIER_RE.fullmatch(value):
        raise PromptQueueValidationError(f"Prompt {kind} identifier is invalid.")
    return value


def _validate_lease_identifiers(
    session_id: Any, prompt_id: Any, lease_token: Any
) -> tuple[str, str, str]:
    return (
        _validate_identifier(session_id, "session", max_length=256),
        _validate_identifier(prompt_id, "prompt", max_length=256),
        _validate_identifier(lease_token, "lease", max_length=256),
    )


def _validate_lease_seconds(value: Any) -> float:
    if isinstance(value, bool):
        raise PromptQueueValidationError("Prompt lease duration is invalid.")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise PromptQueueValidationError("Prompt lease duration is invalid.") from None
    if not math.isfinite(parsed) or parsed < MIN_LEASE_SECONDS or parsed > MAX_LEASE_SECONDS:
        raise PromptQueueValidationError("Prompt lease duration is outside the allowed range.")
    return parsed


def _contains_control_character(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _restrict_permissions(path: Path, mode: int) -> None:
    if os.name == "nt" or not path.exists():
        return
    try:
        path.chmod(mode)
    except OSError:
        # Directory ownership/ACLs may be managed by the host.  SQLite remains
        # usable; deployments can enforce their own ACL policy.
        pass


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DurablePromptQueue",
    "EnqueueResult",
    "PromptIdempotencyConflict",
    "PromptLease",
    "PromptLeaseLost",
    "PromptNotFound",
    "PromptQueueCapacityError",
    "PromptQueueConfig",
    "PromptQueueError",
    "PromptQueueItem",
    "PromptQueueStorageError",
    "PromptQueueValidationError",
    "PromptState",
    "PromptStateError",
    "TERMINAL_STATES",
    "default_prompt_queue_path",
]
