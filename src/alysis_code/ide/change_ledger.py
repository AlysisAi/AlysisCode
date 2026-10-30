from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..branding import canonical_user_data_dir, env_get
from ..git_safe import build_git_process_env
from ..tools.fs import classify_sensitive_path

_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_KINDS = frozenset({"baseline", "turn", "step", "revert", "redo"})
_MAX_DIFF_BYTES = 2 * 1024 * 1024
_DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024
_DEFAULT_MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
_ZERO_OID = "0" * 40
_SESSION_REF_PREFIX = "refs/heads/sessions/"
_METADATA_PREFIX = "Alysis Code-Checkpoint-Metadata: "
_PENDING_OPERATION_VERSION = 1
_CHECKPOINT_METADATA_VERSION = 2
_MAX_OMITTED_PATHS = 10_000
_ALL_PATHS_NON_REVERTIBLE = "*"
_EXCLUDES = """\
/.git/
/.alysis/
**/.env
**/.env.*
!**/.env.example
!**/.env.sample
!**/.env.template
**/*.pem
**/*.key
**/*.p12
**/*.pfx
**/id_rsa
**/id_ed25519
**/credentials.json
**/service-account*.json
"""


class ChangeLedgerError(RuntimeError):
    """A safe, user-displayable checkpoint failure."""


class StaleWorkspaceError(ChangeLedgerError):
    """The workspace no longer matches the state being reversed."""


def _checkpoint_storage_directory(workspace_root: Path, storage_root: Path | None = None) -> Path:
    workspace = workspace_root.expanduser().resolve(strict=True)
    workspace_key = hashlib.sha256(
        os.path.normcase(os.fspath(workspace)).encode("utf-8")
    ).hexdigest()[:24]
    override = str(env_get("ALYSIS_DATA_DIR") or "").strip()
    default_data_dir = Path(override).expanduser() if override else canonical_user_data_dir()
    base = (storage_root or default_data_dir / "checkpoints").expanduser().resolve()
    try:
        base.relative_to(workspace)
    except ValueError:
        return base / workspace_key
    raise ChangeLedgerError("Checkpoint storage must be outside the workspace.")


def checkpoint_storage_exists(workspace_root: Path) -> bool:
    """Check for durable checkpoint state without initializing storage or launching Git."""
    storage = _checkpoint_storage_directory(workspace_root)
    return (storage / "ledger.sqlite3").exists() or (storage / "objects.git").exists()


@dataclass(frozen=True)
class ChangeRecord:
    status: str
    path: str
    previous_path: str | None = None


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    session_id: str
    turn_id: str | None
    step_id: str | None
    parent_id: str | None
    commit_oid: str
    kind: str
    created_at: str
    message: str
    changes: tuple[ChangeRecord, ...]
    omitted_paths: tuple[str, ...]
    non_revertible_paths: tuple[str, ...]
    reverts_id: str | None = None
    redoes_id: str | None = None


@dataclass(frozen=True)
class _StageResult:
    omitted_paths: tuple[str, ...]
    non_revertible_paths: tuple[str, ...]


@dataclass(frozen=True)
class _RestoreMutation:
    rel_path: str
    before_identity: tuple[str, str] | None
    after_identity: tuple[str, str] | None
    displaced_path: Path | None


class ChangeLedger:
    """Durable, external Git-backed checkpoints for one workspace.

    The object database, index, refs, and metadata live below the Alysis Code user
    data directory (or an injected test directory). The developer's repository,
    index, branches, configuration, and hooks are never used or changed.
    """

    def __init__(
        self,
        workspace_root: Path,
        *,
        storage_root: Path | None = None,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
        max_snapshot_bytes: int = _DEFAULT_MAX_SNAPSHOT_BYTES,
        git_timeout_seconds: float = 30.0,
    ) -> None:
        self.workspace_root = workspace_root.expanduser().resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ChangeLedgerError("Checkpoint workspace must be a directory.")
        self.storage_dir = _checkpoint_storage_directory(self.workspace_root, storage_root)
        self.git_dir = self.storage_dir / "objects.git"
        self.index_dir = self.storage_dir / "indexes"
        self.database_path = self.storage_dir / "ledger.sqlite3"
        self.pending_operation_path = self.storage_dir / "pending-operation.json"
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.max_snapshot_bytes = max(self.max_file_bytes, int(max_snapshot_bytes))
        self.git_timeout_seconds = max(1.0, float(git_timeout_seconds))
        self._lock = threading.RLock()
        self._initialize()

    def ensure_baseline(self, session_id: str) -> Checkpoint:
        session = _validate_session_id(session_id)
        with self._lock:
            self._reconcile_storage(session_id=session)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = self._lineage_owner_locked(db, session)
            current = self._head_locked(db, owner)
            if current is not None:
                db.commit()
                return current
            result = self._capture_locked(
                db,
                session_id=owner,
                turn_id=None,
                step_id=None,
                kind="baseline",
                message="Initial workspace baseline",
                paths=None,
            )
            db.commit()
            return result

    def capture(
        self,
        session_id: str,
        *,
        turn_id: str | None,
        step_id: str | None = None,
        kind: str = "turn",
        message: str = "",
        paths: Iterable[str],
    ) -> Checkpoint:
        session = _validate_session_id(session_id)
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in _KINDS:
            raise ChangeLedgerError("Unsupported checkpoint kind.")
        scoped_paths = _normalize_scope_paths(self.workspace_root, paths)
        with self._lock:
            self._reconcile_storage(session_id=session)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = self._lineage_owner_locked(db, session)
            result = self._capture_locked(
                db,
                session_id=owner,
                turn_id=_bounded_label(turn_id),
                step_id=_bounded_label(step_id),
                kind=normalized_kind,
                message=_bounded_message(message),
                paths=scoped_paths,
            )
            db.commit()
            return result

    def list(self, session_id: str, *, limit: int = 100) -> tuple[Checkpoint, ...]:
        session = _validate_session_id(session_id)
        count = min(500, max(1, int(limit)))
        with self._lock:
            self._reconcile_storage(session_id=session)
        with self._connect() as db:
            owner = self._lineage_owner_locked(db, session)
            rows = db.execute(
                "SELECT * FROM checkpoints WHERE session_id = ? ORDER BY sequence DESC LIMIT ?",
                (owner, count),
            ).fetchall()
        return tuple(self._checkpoint_from_row(row) for row in rows)

    def head(self, session_id: str) -> Checkpoint | None:
        session = _validate_session_id(session_id)
        with self._lock:
            self._reconcile_storage(session_id=session)
        with self._connect() as db:
            owner = self._lineage_owner_locked(db, session)
            return self._head_locked(db, owner)

    def diff(
        self,
        session_id: str,
        checkpoint_id: str,
        *,
        max_bytes: int = _MAX_DIFF_BYTES,
    ) -> dict[str, Any]:
        checkpoint = self.get_for_session(session_id, checkpoint_id)
        if checkpoint.parent_id is None:
            args = ["show", "--format=", "--binary", checkpoint.commit_oid]
        else:
            parent = self.get_for_session(session_id, checkpoint.parent_id)
            args = ["diff", "--binary", "--no-ext-diff", parent.commit_oid, checkpoint.commit_oid]
        raw = self._git(args, check=True).stdout
        cap = min(16 * 1024 * 1024, max(1024, int(max_bytes)))
        truncated = len(raw) > cap
        visible = raw[:cap]
        return {
            "checkpoint_id": checkpoint.checkpoint_id,
            "diff": visible.decode("utf-8", errors="replace"),
            "truncated": truncated,
            "original_bytes": len(raw),
            "changes": [record.__dict__ for record in checkpoint.changes],
        }

    def get(self, checkpoint_id: str) -> Checkpoint:
        clean = str(checkpoint_id or "").strip()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id = ?", (clean,)
            ).fetchone()
        if row is None:
            raise ChangeLedgerError("Checkpoint was not found.")
        return self._checkpoint_from_row(row)

    def get_for_session(self, session_id: str, checkpoint_id: str) -> Checkpoint:
        session = _validate_session_id(session_id)
        clean = str(checkpoint_id or "").strip()
        with self._lock:
            self._reconcile_storage(session_id=session)
        with self._connect() as db:
            owner = self._lineage_owner_locked(db, session)
            row = db.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id = ? AND session_id = ?",
                (clean, owner),
            ).fetchone()
        if row is None:
            raise ChangeLedgerError("Checkpoint was not found for this session.")
        return self._checkpoint_from_row(row)

    def adopt_session(self, session_id: str, source_session_id: str) -> str | None:
        """Bind a fresh session to an existing durable checkpoint lineage.

        The binding is explicit, durable, and one-way. A session that already
        captured a turn cannot silently exchange its history for another one.
        Repeating the same adoption is idempotent.
        """

        session = _validate_session_id(session_id)
        source = _validate_session_id(source_session_id)
        with self._lock:
            self._reconcile_storage(session_id=source)
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                source_owner = self._lineage_owner_locked(db, source)
                source_head = self._head_locked(db, source_owner)
                if source_head is None:
                    db.commit()
                    return None

                alias = db.execute(
                    "SELECT owner_session_id FROM session_lineages WHERE session_id = ?",
                    (session,),
                ).fetchone()
                if alias is not None:
                    existing_owner = self._lineage_owner_locked(db, session)
                    if existing_owner != source_owner:
                        raise ChangeLedgerError(
                            "Session is already bound to a different checkpoint lineage."
                        )
                    db.commit()
                    return source_owner

                if session == source_owner:
                    db.commit()
                    return source_owner

                own_rows = db.execute(
                    "SELECT kind, parent_id FROM checkpoints WHERE session_id = ? "
                    "ORDER BY sequence ASC",
                    (session,),
                ).fetchall()
                if own_rows and not (
                    len(own_rows) == 1
                    and own_rows[0]["kind"] == "baseline"
                    and own_rows[0]["parent_id"] is None
                ):
                    raise ChangeLedgerError(
                        "Session already has checkpoint history and cannot adopt another lineage."
                    )
                db.execute(
                    "INSERT INTO session_lineages(session_id, owner_session_id) VALUES (?, ?)",
                    (session, source_owner),
                )
                db.commit()
                return source_owner

    def revert(self, session_id: str, checkpoint_id: str) -> Checkpoint:
        session = _validate_session_id(session_id)
        with self._lock:
            self._reconcile_storage(session_id=session)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = self._lineage_owner_locked(db, session)
            target = self._checkpoint_locked(db, checkpoint_id)
            head = self._head_locked(db, owner)
            if (
                target.session_id != owner
                or head is None
                or head.checkpoint_id != target.checkpoint_id
            ):
                raise ChangeLedgerError(
                    "Only the current session checkpoint can be reverted safely."
                )
            if target.parent_id is None:
                raise ChangeLedgerError("The initial workspace baseline cannot be reverted.")
            parent = self._checkpoint_locked(db, target.parent_id)
            paths = _record_paths(target.changes)
            self._require_paths_revertible(paths, target, parent)
            self._require_paths_match(target.commit_oid, paths)
            pending = self._pending_restore_payload(
                session_id=owner,
                action="revert",
                source=target,
                restore_oid=parent.commit_oid,
                paths=paths,
                turn_id=target.turn_id,
                step_id=target.step_id,
                kind="revert",
                message=f"Revert {target.checkpoint_id}",
                reverts_id=target.checkpoint_id,
            )
            self._write_pending_operation(pending)
            self._restore_paths(parent.commit_oid, paths, rollback_oid=target.commit_oid)
            result = self._capture_locked(
                db,
                session_id=owner,
                turn_id=target.turn_id,
                step_id=target.step_id,
                kind="revert",
                message=f"Revert {target.checkpoint_id}",
                reverts_id=target.checkpoint_id,
                paths=paths,
                checkpoint_id=str(pending["checkpoint_id"]),
            )
            db.commit()
            self.pending_operation_path.unlink(missing_ok=True)
            return result

    def redo(self, session_id: str, *, expected_checkpoint_id: str | None = None) -> Checkpoint:
        session = _validate_session_id(session_id)
        with self._lock:
            self._reconcile_storage(session_id=session)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = self._lineage_owner_locked(db, session)
            head = self._head_locked(db, owner)
            if head is None or head.kind != "revert" or not head.reverts_id:
                raise ChangeLedgerError("The current checkpoint is not redoable.")
            if expected_checkpoint_id is not None and head.checkpoint_id != expected_checkpoint_id:
                raise ChangeLedgerError("The reviewed checkpoint is no longer current.")
            original = self._checkpoint_locked(db, head.reverts_id)
            paths = _record_paths(original.changes)
            self._require_paths_revertible(paths, original, head)
            self._require_paths_match(head.commit_oid, paths)
            pending = self._pending_restore_payload(
                session_id=owner,
                action="redo",
                source=head,
                restore_oid=original.commit_oid,
                paths=paths,
                turn_id=original.turn_id,
                step_id=original.step_id,
                kind="redo",
                message=f"Redo {original.checkpoint_id}",
                redoes_id=original.checkpoint_id,
            )
            self._write_pending_operation(pending)
            self._restore_paths(original.commit_oid, paths, rollback_oid=head.commit_oid)
            result = self._capture_locked(
                db,
                session_id=owner,
                turn_id=original.turn_id,
                step_id=original.step_id,
                kind="redo",
                message=f"Redo {original.checkpoint_id}",
                redoes_id=original.checkpoint_id,
                paths=paths,
                checkpoint_id=str(pending["checkpoint_id"]),
            )
            db.commit()
            self.pending_operation_path.unlink(missing_ok=True)
            return result

    def create_branch(self, session_id: str, name: str, checkpoint_id: str) -> str:
        checkpoint = self.get_for_session(session_id, checkpoint_id)
        clean = re.sub(r"[^A-Za-z0-9._/-]+", "-", str(name or "").strip()).strip("-./")
        if not clean or len(clean) > 120 or ".." in clean or clean.endswith(".lock"):
            raise ChangeLedgerError("Invalid checkpoint branch name.")
        ref = f"refs/heads/checkpoints/{clean}"
        self._git(["check-ref-format", ref], check=True)
        self._git(["update-ref", ref, checkpoint.commit_oid], check=True)
        return ref

    def _initialize(self) -> None:
        # Windows process creation rejects an oversized working directory.
        # Below it, Git uses a short relative GIT_DIR plus longpaths for objects.
        if os.name == "nt" and len(os.fspath(self.storage_dir)) >= 260:
            raise ChangeLedgerError(
                "Windows Git checkpoint storage path is too long. "
                "Choose a shorter Alysis data directory before making changes."
            )
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        if not self.git_dir.exists():
            try:
                result = subprocess.run(
                    [
                        "git",
                        *(["-c", "core.longpaths=true"] if os.name == "nt" else []),
                        "init",
                        "--bare",
                        "--quiet",
                        "objects.git",
                    ],
                    cwd=self.storage_dir,
                    capture_output=True,
                    timeout=self.git_timeout_seconds,
                    check=False,
                    env=build_git_process_env(),
                )
            except (OSError, subprocess.TimeoutExpired):
                raise ChangeLedgerError(
                    "Checkpoint storage could not start Git. Install Git and check that it can run."
                ) from None
            if result.returncode != 0:
                raise ChangeLedgerError("Unable to initialize external checkpoint storage.")
        self._object_hash_name = (
            self._git(["rev-parse", "--show-object-format"], check=True).stdout.decode().strip()
        )
        if self._object_hash_name not in {"sha1", "sha256"}:
            raise ChangeLedgerError("Unsupported checkpoint Git object format.")
        info_dir = self.git_dir / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        exclude_path = info_dir / "exclude"
        if not exclude_path.exists() or exclude_path.read_text(encoding="utf-8") != _EXCLUDES:
            _atomic_write(exclude_path, _EXCLUDES.encode("utf-8"), mode=None)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    checkpoint_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    turn_id TEXT,
                    step_id TEXT,
                    parent_id TEXT,
                    commit_oid TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    message TEXT NOT NULL,
                    changes_json TEXT NOT NULL,
                    omitted_json TEXT NOT NULL,
                    non_revertible_json TEXT NOT NULL DEFAULT '[]',
                    reverts_id TEXT,
                    redoes_id TEXT
                );
                CREATE INDEX IF NOT EXISTS checkpoint_session_sequence
                    ON checkpoints(session_id, sequence DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS checkpoint_commit_oid
                    ON checkpoints(commit_oid);
                CREATE TABLE IF NOT EXISTS session_heads (
                    session_id TEXT PRIMARY KEY,
                    checkpoint_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_lineages (
                    session_id TEXT PRIMARY KEY,
                    owner_session_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ledger_schema (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {
                str(row["name"]) for row in db.execute("PRAGMA table_info(checkpoints)").fetchall()
            }
            added_non_revertible_column = "non_revertible_json" not in columns
            if added_non_revertible_column:
                db.execute(
                    "ALTER TABLE checkpoints ADD COLUMN non_revertible_json "
                    "TEXT NOT NULL DEFAULT '[]'"
                )
            schema_row = db.execute(
                "SELECT value FROM ledger_schema WHERE key = 'omission_safety_version'"
            ).fetchone()
            migration_required = schema_row is None or str(schema_row["value"]) != "2"
            self._backfill_non_revertible_metadata_locked(
                db,
                migration_required=migration_required,
            )
            db.execute(
                "INSERT INTO ledger_schema(key, value) VALUES ('omission_safety_version', '2') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
            db.commit()
        self._reconcile_storage()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database_path, timeout=30.0)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = FULL")
            db.execute("PRAGMA foreign_keys = ON")
            yield db
        finally:
            db.close()

    def _backfill_non_revertible_metadata_locked(
        self,
        db: sqlite3.Connection,
        *,
        migration_required: bool,
    ) -> None:
        """Migrate omission safety without pretending old snapshots are complete.

        Version-one baselines did not catalog every ignored or sensitive path.
        Their exact omission set cannot be reconstructed after the fact, so the
        only honest migration is to make the lineage globally non-revertible.
        New metadata carries an exact, cumulative protected-path set.
        """

        if not migration_required:
            return

        inherited_by_id: dict[str, set[str]] = {}
        rows = db.execute("SELECT * FROM checkpoints ORDER BY sequence ASC").fetchall()
        for row in rows:
            parent_id = str(row["parent_id"] or "")
            protected = set(inherited_by_id.get(parent_id, set()))
            stored = _metadata_path_set(row["non_revertible_json"])
            protected.update(stored)
            metadata, _message, _created_at = self._commit_metadata(str(row["commit_oid"]))
            version = int(metadata.get("version") or 0)
            if row["parent_id"] is None and version < 2:
                protected.add(_ALL_PATHS_NON_REVERTIBLE)
            else:
                omitted = _metadata_path_set(row["omitted_json"])
                for path in omitted:
                    if (
                        path.endswith("/")
                        or self._tree_identity(str(row["commit_oid"]), path) is None
                    ):
                        protected.add(path)
            normalized = sorted(protected)
            db.execute(
                "UPDATE checkpoints SET non_revertible_json = ? WHERE checkpoint_id = ?",
                (json.dumps(normalized, separators=(",", ":")), row["checkpoint_id"]),
            )
            inherited_by_id[str(row["checkpoint_id"])] = set(normalized)

    def _lineage_owner_locked(self, db: sqlite3.Connection, session_id: str) -> str:
        current = _validate_session_id(session_id)
        seen: set[str] = set()
        while True:
            if current in seen:
                raise ChangeLedgerError("Checkpoint session lineage contains a cycle.")
            seen.add(current)
            row = db.execute(
                "SELECT owner_session_id FROM session_lineages WHERE session_id = ?",
                (current,),
            ).fetchone()
            if row is None:
                return current
            current = _validate_session_id(str(row["owner_session_id"] or ""))

    def _reconcile_storage(self, *, session_id: str | None = None) -> None:
        """Repair durable ref/database and restore-operation crash windows.

        Git refs are published before SQLite metadata is committed, while restore
        intents are journaled before any workspace path is displaced.  Holding a
        SQLite write transaction here serializes recovery with every live ledger
        mutation; after a process crash, the ref and journal contain enough data
        to either finish the operation or roll its partial workspace changes back.
        """

        pending = self._read_pending_operation()
        sessions: set[str] = set()
        if session_id is not None:
            sessions.add(_validate_session_id(session_id))
        if pending is not None:
            sessions.add(_validate_session_id(str(pending.get("session_id") or "")))

        clear_pending = False
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            sessions.update(
                str(row["session_id"])
                for row in db.execute("SELECT DISTINCT session_id FROM checkpoints").fetchall()
            )
            # Read inside the write transaction: refs move only under it, as SQLite does.
            ref_oids = self._session_ref_oids()
            for session in sorted(sessions):
                self._reconcile_session_ref_locked(db, session, ref_oids)
            if pending is not None:
                clear_pending = self._reconcile_pending_restore_locked(db, pending)
            db.commit()
        if clear_pending:
            self.pending_operation_path.unlink(missing_ok=True)

    def _reconcile_session_ref_locked(
        self,
        db: sqlite3.Connection,
        session_id: str,
        ref_oids: dict[str, str],
    ) -> None:
        head = self._head_locked(db, session_id)
        ref = f"{_SESSION_REF_PREFIX}{_session_key(session_id)}"
        ref_oid = ref_oids.get(ref)
        if ref_oid is None and head is not None:
            # A head without its ref is repaired below; read that one ref as before first.
            ref_oid = self._ref_oid(ref)
        if head is None and ref_oid is None:
            return
        if head is not None and ref_oid == head.commit_oid:
            return
        if ref_oid is None:
            assert head is not None
            self._git(["update-ref", ref, head.commit_oid, _ZERO_OID], check=True)
            return
        if head is None:
            commits = self._revision_list(ref_oid)
            self._recover_ref_commits_locked(db, session_id, parent=None, commits=commits)
            return
        if self._is_ancestor(head.commit_oid, ref_oid):
            commits = self._revision_list(f"{head.commit_oid}..{ref_oid}")
            self._recover_ref_commits_locked(db, session_id, parent=head, commits=commits)
            return
        if self._is_ancestor(ref_oid, head.commit_oid):
            self._git(["update-ref", ref, head.commit_oid, ref_oid], check=True)
            return
        raise ChangeLedgerError(
            "Checkpoint metadata and its durable Git ref diverged; refusing unsafe recovery."
        )

    def _recover_ref_commits_locked(
        self,
        db: sqlite3.Connection,
        session_id: str,
        *,
        parent: Checkpoint | None,
        commits: tuple[str, ...],
    ) -> None:
        previous = parent
        previous_oid = parent.commit_oid if parent is not None else None
        for commit_oid in commits:
            commit_parent = self._commit_parent_oid(commit_oid)
            if commit_parent != previous_oid:
                raise ChangeLedgerError(
                    "Checkpoint ref history is not a linear continuation of SQLite metadata."
                )
            metadata, fallback_message, committed_at = self._commit_metadata(commit_oid)
            metadata_session = str(metadata.get("session_id") or session_id)
            if metadata_session != session_id:
                raise ChangeLedgerError("Checkpoint ref metadata belongs to another session.")
            checkpoint_id = str(metadata.get("checkpoint_id") or uuid.uuid4())
            existing = db.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id = ? OR commit_oid = ?",
                (checkpoint_id, commit_oid),
            ).fetchone()
            if existing is not None:
                recovered = self._checkpoint_from_row(existing)
                if recovered.commit_oid != commit_oid or recovered.session_id != session_id:
                    raise ChangeLedgerError("Checkpoint recovery found conflicting metadata.")
                previous = recovered
                previous_oid = commit_oid
                continue
            raw_kind = str(metadata.get("kind") or ("baseline" if previous is None else "turn"))
            kind = raw_kind if raw_kind in _KINDS else "turn"
            message = _bounded_message(str(metadata.get("message") or fallback_message))
            changes = self._changes_between(previous_oid, commit_oid)
            omitted_value = metadata.get("omitted_paths")
            omitted = sorted(_metadata_path_set(omitted_value or []))
            protected = set(previous.non_revertible_paths if previous is not None else ())
            protected_value = metadata.get("non_revertible_paths")
            if isinstance(protected_value, list):
                protected.update(_metadata_path_set(protected_value))
            if previous is None and int(metadata.get("version") or 0) < 2:
                protected.add(_ALL_PATHS_NON_REVERTIBLE)
            else:
                for path in omitted:
                    if path.endswith("/") or self._tree_identity(commit_oid, path) is None:
                        protected.add(path)
            db.execute(
                "INSERT INTO checkpoints (checkpoint_id, session_id, turn_id, step_id, parent_id, "
                "commit_oid, kind, created_at, message, changes_json, omitted_json, "
                "non_revertible_json, reverts_id, redoes_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    checkpoint_id,
                    session_id,
                    _bounded_label(metadata.get("turn_id")),
                    _bounded_label(metadata.get("step_id")),
                    previous.checkpoint_id if previous is not None else None,
                    commit_oid,
                    kind,
                    str(metadata.get("created_at") or committed_at),
                    message,
                    json.dumps([record.__dict__ for record in changes], separators=(",", ":")),
                    json.dumps(omitted, separators=(",", ":")),
                    json.dumps(sorted(protected), separators=(",", ":")),
                    _bounded_label(metadata.get("reverts_id")),
                    _bounded_label(metadata.get("redoes_id")),
                ),
            )
            previous = self._checkpoint_locked(db, checkpoint_id)
            previous_oid = commit_oid
        if previous is not None:
            db.execute(
                "INSERT INTO session_heads(session_id, checkpoint_id) VALUES (?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET checkpoint_id = excluded.checkpoint_id",
                (session_id, previous.checkpoint_id),
            )

    def _reconcile_pending_restore_locked(
        self,
        db: sqlite3.Connection,
        pending: dict[str, Any],
    ) -> bool:
        session_id = _validate_session_id(str(pending.get("session_id") or ""))
        checkpoint_id = str(pending.get("checkpoint_id") or "")
        finalized = db.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE checkpoint_id = ?",
            (checkpoint_id,),
        ).fetchone()
        if finalized is not None:
            return True

        source = self._checkpoint_locked(db, str(pending.get("source_checkpoint_id") or ""))
        head = self._head_locked(db, session_id)
        if head is None or head.checkpoint_id != source.checkpoint_id:
            raise ChangeLedgerError(
                "A pending checkpoint restore no longer matches the recorded session head."
            )
        source_oid = str(pending.get("source_oid") or "")
        restore_oid = str(pending.get("restore_oid") or "")
        if source.commit_oid != source_oid or not restore_oid:
            raise ChangeLedgerError("Pending checkpoint recovery metadata is invalid.")
        paths = _normalize_scope_paths(self.workspace_root, pending.get("paths") or [])
        protected_checkpoints = [source]
        related_id = str(pending.get("redoes_id") or pending.get("reverts_id") or "").strip()
        if related_id:
            protected_checkpoints.append(self._checkpoint_locked(db, related_id))
        self._require_paths_revertible(paths, *protected_checkpoints)

        states: dict[str, str] = {}
        displaced_entries: dict[str, tuple[Path, ...]] = {}
        for path in paths:
            current = self._worktree_identity(path)
            source_identity = self._tree_identity(source_oid, path)
            restore_identity = self._tree_identity(restore_oid, path)
            displaced = self._matching_displaced_entries(path, source_identity)
            displaced_entries[path] = displaced
            if current is None and source_identity is not None and displaced:
                # The process died after moving the source inode aside but before
                # publishing the requested tree entry. Restore the durable source
                # namespace first; the operation can then be rolled back honestly.
                if not _publish_entry_no_replace(
                    displaced[0], _safe_workspace_path(self.workspace_root, path)
                ):
                    raise ChangeLedgerError(
                        "A workspace path was claimed while checkpoint recovery was restoring it."
                    )
                current = self._worktree_identity(path)
            if current == restore_identity:
                states[path] = "restored"
            elif current == source_identity:
                states[path] = "source"
            else:
                states[path] = "external"

        if all(state == "restored" for state in states.values()):
            self._capture_locked(
                db,
                session_id=session_id,
                turn_id=_bounded_label(pending.get("turn_id")),
                step_id=_bounded_label(pending.get("step_id")),
                kind=str(pending.get("kind") or "turn"),
                message=_bounded_message(str(pending.get("message") or "Recovered checkpoint")),
                reverts_id=_bounded_label(pending.get("reverts_id")),
                redoes_id=_bounded_label(pending.get("redoes_id")),
                paths=paths,
                checkpoint_id=checkpoint_id,
            )
            self._remove_recovered_displaced_entries(displaced_entries)
            return True

        rollback_paths = tuple(
            path
            for path, state in states.items()
            if state == "restored"
            and self._tree_identity(source_oid, path) != self._tree_identity(restore_oid, path)
        )
        if rollback_paths:
            self._restore_paths(source_oid, rollback_paths, rollback_oid=restore_oid)
        self._remove_recovered_displaced_entries(displaced_entries)
        # Paths changed by a human or another process are deliberately preserved.
        # The SQLite/ref head remains at ``source`` so a later user-requested
        # revert will surface the ordinary stale-workspace conflict.
        return True

    def _matching_displaced_entries(
        self,
        rel_path: str,
        source_identity: tuple[str, str] | None,
    ) -> tuple[Path, ...]:
        if source_identity is None:
            return ()
        target = _safe_workspace_path(self.workspace_root, rel_path)
        prefix = f".{target.name}."
        matches: list[Path] = []
        try:
            siblings = tuple(target.parent.iterdir())
        except OSError:
            return ()
        for candidate in siblings:
            if (
                candidate.name.startswith(prefix)
                and candidate.name.endswith(".displaced")
                and self._path_identity(candidate) == source_identity
            ):
                matches.append(candidate)
        return tuple(matches)

    @staticmethod
    def _remove_recovered_displaced_entries(entries: dict[str, tuple[Path, ...]]) -> None:
        for candidates in entries.values():
            for candidate in candidates:
                candidate.unlink(missing_ok=True)

    def _pending_restore_payload(
        self,
        *,
        session_id: str,
        action: str,
        source: Checkpoint,
        restore_oid: str,
        paths: tuple[str, ...],
        turn_id: str | None,
        step_id: str | None,
        kind: str,
        message: str,
        reverts_id: str | None = None,
        redoes_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "version": _PENDING_OPERATION_VERSION,
            "operation_id": str(uuid.uuid4()),
            "checkpoint_id": str(uuid.uuid4()),
            "session_id": session_id,
            "action": action,
            "source_checkpoint_id": source.checkpoint_id,
            "source_oid": source.commit_oid,
            "restore_oid": restore_oid,
            "paths": list(paths),
            "turn_id": turn_id,
            "step_id": step_id,
            "kind": kind,
            "message": message,
            "reverts_id": reverts_id,
            "redoes_id": redoes_id,
        }

    def _write_pending_operation(self, payload: dict[str, Any]) -> None:
        _atomic_write(
            self.pending_operation_path,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            mode=0o600,
        )

    def _read_pending_operation(self) -> dict[str, Any] | None:
        if not self.pending_operation_path.exists():
            return None
        try:
            payload = json.loads(self.pending_operation_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ChangeLedgerError("Pending checkpoint recovery metadata is unreadable.") from exc
        if not isinstance(payload, dict) or payload.get("version") != _PENDING_OPERATION_VERSION:
            raise ChangeLedgerError("Pending checkpoint recovery metadata is invalid.")
        return payload

    def _ref_oid(self, ref: str) -> str | None:
        result = self._git(["rev-parse", "--verify", ref], check=False)
        if result.returncode != 0:
            return None
        return result.stdout.decode("ascii", errors="strict").strip()

    def _session_ref_oids(self) -> dict[str, str]:
        """Every session ref and the commit it names, read with one Git process.

        Reconciling looks at every session the ledger has held, on every ledger
        operation. One ``rev-parse`` per session cost a process start each: on
        Windows 60-100 ms, so 25 sessions in a project made a resumed chat wait
        about 3.5 s, and the wait grew with each new session.
        """
        output = self._git(
            ["for-each-ref", "--format=%(objectname) %(refname)", _SESSION_REF_PREFIX],
            check=True,
        ).stdout
        refs: dict[str, str] = {}
        for line in output.decode("utf-8", errors="replace").splitlines():
            oid, _, name = line.partition(" ")
            if oid and name:
                refs[name] = oid
        return refs

    def _revision_list(self, revision: str) -> tuple[str, ...]:
        output = self._git(["rev-list", "--reverse", revision], check=True).stdout
        return tuple(line for line in output.decode("ascii").splitlines() if line)

    def _is_ancestor(self, older_oid: str, newer_oid: str) -> bool:
        result = self._git(["merge-base", "--is-ancestor", older_oid, newer_oid], check=False)
        if result.returncode not in {0, 1}:
            raise ChangeLedgerError("Checkpoint Git ancestry could not be verified.")
        return result.returncode == 0

    def _commit_parent_oid(self, commit_oid: str) -> str | None:
        output = self._git(["rev-list", "--parents", "-n", "1", commit_oid], check=True).stdout
        parts = output.decode("ascii").strip().split()
        if not parts or parts[0] != commit_oid or len(parts) > 2:
            raise ChangeLedgerError("Checkpoint commit history is invalid.")
        return parts[1] if len(parts) == 2 else None

    def _commit_metadata(self, commit_oid: str) -> tuple[dict[str, Any], str, str]:
        output = self._git(
            ["show", "-s", "--format=%B%x00%cI", commit_oid],
            check=True,
        ).stdout.decode("utf-8", errors="replace")
        message, separator, committed_at = output.rpartition("\x00")
        if not separator:
            raise ChangeLedgerError("Checkpoint commit metadata is invalid.")
        metadata: dict[str, Any] = {}
        display_lines: list[str] = []
        for line in message.rstrip().splitlines():
            if line.startswith(_METADATA_PREFIX):
                encoded = line[len(_METADATA_PREFIX) :].strip()
                try:
                    decoded = base64.urlsafe_b64decode(encoded.encode("ascii"))
                    parsed = json.loads(decoded.decode("utf-8"))
                except (ValueError, UnicodeError, json.JSONDecodeError):
                    raise ChangeLedgerError("Checkpoint commit metadata is invalid.") from None
                if not isinstance(parsed, dict):
                    raise ChangeLedgerError("Checkpoint commit metadata is invalid.")
                metadata = parsed
            else:
                display_lines.append(line)
        return metadata, _bounded_message("\n".join(display_lines).strip()), committed_at.strip()

    def _capture_locked(
        self,
        db: sqlite3.Connection,
        *,
        session_id: str,
        turn_id: str | None,
        step_id: str | None,
        kind: str,
        message: str,
        reverts_id: str | None = None,
        redoes_id: str | None = None,
        paths: tuple[str, ...] | None,
        checkpoint_id: str | None = None,
    ) -> Checkpoint:
        parent = self._head_locked(db, session_id)
        parent_oid = parent.commit_oid if parent else None
        env = self._session_git_env(session_id)
        if parent_oid:
            self._git(["read-tree", parent_oid], env=env, check=True)
        else:
            self._git(["read-tree", "--empty"], env=env, check=True)
        staged = self._stage_workspace(
            env=env,
            paths=paths,
            parent_oid=parent_oid,
            inherited_non_revertible=(parent.non_revertible_paths if parent is not None else ()),
        )
        tree_oid = self._git(["write-tree"], env=env, check=True).stdout.decode().strip()
        commit_args = ["commit-tree", tree_oid]
        if parent_oid:
            commit_args.extend(["-p", parent_oid])
        created_at = datetime.now(UTC).isoformat()
        commit_env = dict(env)
        commit_env.update(
            {
                "GIT_AUTHOR_NAME": "Alysis Code Checkpoint",
                "GIT_AUTHOR_EMAIL": "checkpoint@localhost",
                "GIT_COMMITTER_NAME": "Alysis Code Checkpoint",
                "GIT_COMMITTER_EMAIL": "checkpoint@localhost",
                "GIT_AUTHOR_DATE": created_at,
                "GIT_COMMITTER_DATE": created_at,
            }
        )
        safe_message = message or f"{kind} checkpoint"
        resolved_checkpoint_id = checkpoint_id or str(uuid.uuid4())
        commit_metadata = {
            "version": _CHECKPOINT_METADATA_VERSION,
            "checkpoint_id": resolved_checkpoint_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "step_id": step_id,
            "kind": kind,
            "created_at": created_at,
            "message": safe_message,
            "omitted_paths": list(staged.omitted_paths),
            "non_revertible_paths": list(staged.non_revertible_paths),
            "reverts_id": reverts_id,
            "redoes_id": redoes_id,
        }
        encoded_metadata = base64.urlsafe_b64encode(
            json.dumps(commit_metadata, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        commit_message = f"{safe_message}\n\n{_METADATA_PREFIX}{encoded_metadata}"
        commit_oid = (
            self._git(
                commit_args,
                env=commit_env,
                input_bytes=commit_message.encode("utf-8"),
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        ref = f"{_SESSION_REF_PREFIX}{_session_key(session_id)}"
        update_args = ["update-ref", ref, commit_oid, parent_oid or _ZERO_OID]
        self._git(update_args, check=True)
        changes = self._changes_between(parent_oid, commit_oid)
        db.execute(
            "INSERT INTO checkpoints (checkpoint_id, session_id, turn_id, step_id, parent_id, "
            "commit_oid, kind, created_at, message, changes_json, omitted_json, "
            "non_revertible_json, reverts_id, redoes_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                resolved_checkpoint_id,
                session_id,
                turn_id,
                step_id,
                parent.checkpoint_id if parent else None,
                commit_oid,
                kind,
                created_at,
                safe_message,
                json.dumps([record.__dict__ for record in changes], separators=(",", ":")),
                json.dumps(staged.omitted_paths, separators=(",", ":")),
                json.dumps(staged.non_revertible_paths, separators=(",", ":")),
                reverts_id,
                redoes_id,
            ),
        )
        db.execute(
            "INSERT INTO session_heads(session_id, checkpoint_id) VALUES (?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET checkpoint_id = excluded.checkpoint_id",
            (session_id, resolved_checkpoint_id),
        )
        return self._checkpoint_locked(db, resolved_checkpoint_id)

    def _stage_workspace(
        self,
        *,
        env: dict[str, str],
        paths: tuple[str, ...] | None,
        parent_oid: str | None,
        inherited_non_revertible: tuple[str, ...],
    ) -> _StageResult:
        if paths == ():
            return _StageResult((), tuple(sorted(set(inherited_non_revertible))))
        scopes = [None] if paths is None else _batches(list(paths))
        tracked: set[str] = set()
        candidate_set: set[str] = set()
        ignored_set: set[str] = set()
        for scope in scopes:
            pathspec = [] if scope is None else ["--", *scope]
            tracked_raw = self._git(["ls-files", "-z", *pathspec], env=env, check=True).stdout
            tracked.update(_decode_git_path(part) for part in tracked_raw.split(b"\0") if part)
            candidates_raw = self._git(
                ["ls-files", "-co", "--exclude-standard", "-z", *pathspec],
                env=env,
                check=True,
            ).stdout
            candidate_set.update(
                _decode_git_path(part) for part in candidates_raw.split(b"\0") if part
            )
            ignored_raw = self._git(
                [
                    "ls-files",
                    "-o",
                    "-i",
                    "--exclude-standard",
                    "--directory",
                    "-z",
                    *pathspec,
                ],
                env=env,
                check=True,
            ).stdout
            ignored_set.update(_decode_git_path(part) for part in ignored_raw.split(b"\0") if part)
        candidates = sorted(candidate_set)
        protected = set(inherited_non_revertible)
        omitted: set[str] = set(ignored_set)
        deleted: list[str] = []
        for path in sorted(tracked):
            if os.path.lexists(self.workspace_root / path):
                continue
            if _is_runtime_or_sensitive(path) or _path_is_non_revertible(path, protected):
                omitted.add(path)
            else:
                deleted.append(path)
        expected_identities = {path: None for path in deleted}
        for batch in _batches(deleted):
            self._git(["update-index", "--remove", "--", *batch], env=env, check=True)

        accepted: list[str] = []
        total = 0
        for rel_path in candidates:
            path = self.workspace_root / rel_path
            if (
                _is_runtime_or_sensitive(rel_path)
                or _path_is_non_revertible(rel_path, protected)
                or not os.path.lexists(path)
            ):
                omitted.add(rel_path)
                continue
            try:
                size = (
                    len(os.readlink(path).encode("utf-8"))
                    if path.is_symlink()
                    else path.stat().st_size
                )
            except OSError:
                omitted.add(rel_path)
                continue
            if size > self.max_file_bytes or total + size > self.max_snapshot_bytes:
                omitted.add(rel_path)
                continue
            identity = self._worktree_identity(rel_path)
            if identity is None or identity == ("unstable", "unstable"):
                raise StaleWorkspaceError(
                    "Workspace files changed while the checkpoint was being captured; retry."
                )
            total += size
            accepted.append(rel_path)
            expected_identities[rel_path] = identity
        for batch in _batches(accepted):
            self._git(["add", "--", *batch], env=env, check=True)
        stale = [
            path
            for path, expected in expected_identities.items()
            if self._worktree_identity(path) != expected
        ]
        if stale:
            raise StaleWorkspaceError(
                "Workspace files changed while the checkpoint was being captured; retry."
            )
        for path in omitted:
            if (
                path.endswith("/")
                or parent_oid is None
                or self._tree_identity(parent_oid, path) is None
            ):
                protected.add(path)
        if len(omitted) > _MAX_OMITTED_PATHS or len(protected) > _MAX_OMITTED_PATHS:
            raise ChangeLedgerError(
                "Checkpoint omission metadata exceeds the safe limit; refusing an incomplete snapshot."
            )
        return _StageResult(tuple(sorted(omitted)), tuple(sorted(protected)))

    def _changes_between(self, parent_oid: str | None, commit_oid: str) -> tuple[ChangeRecord, ...]:
        if parent_oid:
            args = [
                "diff-tree",
                "--no-commit-id",
                "--name-status",
                "-r",
                "-z",
                parent_oid,
                commit_oid,
            ]
        else:
            args = [
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-status",
                "-r",
                "-z",
                commit_oid,
            ]
        parts = [part for part in self._git(args, check=True).stdout.split(b"\0") if part]
        records: list[ChangeRecord] = []
        index = 0
        while index < len(parts):
            status_text = parts[index].decode("ascii", errors="replace")
            index += 1
            status_code = status_text[:1]
            if status_code in {"R", "C"} and index + 1 < len(parts):
                old_path = _decode_git_path(parts[index])
                new_path = _decode_git_path(parts[index + 1])
                index += 2
                records.append(
                    ChangeRecord(status=status_code, path=new_path, previous_path=old_path)
                )
            elif index < len(parts):
                records.append(
                    ChangeRecord(status=status_code, path=_decode_git_path(parts[index]))
                )
                index += 1
        return tuple(records)

    @staticmethod
    def _require_paths_revertible(paths: tuple[str, ...], *checkpoints: Checkpoint) -> None:
        protected = {path for checkpoint in checkpoints for path in checkpoint.non_revertible_paths}
        unsafe = [path for path in paths if _path_is_non_revertible(path, protected)]
        if unsafe:
            raise ChangeLedgerError(
                "Checkpoint contains paths whose earlier bytes were not captured; "
                "refusing a non-revertible restore."
            )

    def _require_paths_match(self, commit_oid: str, paths: tuple[str, ...]) -> None:
        stale = [
            path
            for path in paths
            if self._worktree_identity(path) != self._tree_identity(commit_oid, path)
        ]
        if stale:
            raise StaleWorkspaceError(
                "Workspace files changed after the checkpoint; refusing to overwrite newer work."
            )

    def _restore_paths(self, commit_oid: str, paths: tuple[str, ...], *, rollback_oid: str) -> None:
        restored: list[_RestoreMutation] = []
        try:
            for rel_path in paths:
                restored.append(
                    self._restore_one(
                        commit_oid,
                        rel_path,
                        expected_identity=self._tree_identity(rollback_oid, rel_path),
                    )
                )
        except Exception as exc:
            rollback_failed = False
            for mutation in reversed(restored):
                try:
                    self._rollback_restore(mutation)
                except Exception:
                    rollback_failed = True
            if rollback_failed:
                raise ChangeLedgerError(
                    "A concurrent edit interrupted checkpoint restore and automatic rollback; "
                    "newer workspace content was preserved."
                ) from exc
            if isinstance(exc, ChangeLedgerError):
                raise
            raise ChangeLedgerError("Unable to restore the checkpoint safely.") from None
        for mutation in restored:
            self._finalize_restore(mutation)

    def _restore_one(
        self,
        commit_oid: str,
        rel_path: str,
        *,
        expected_identity: tuple[str, str] | None = None,
    ) -> _RestoreMutation:
        target = _safe_workspace_path(self.workspace_root, rel_path)
        identity = self._tree_identity(commit_oid, rel_path)
        blob: bytes | None = None
        if identity is not None:
            mode, _oid = identity
            if mode == "160000":
                raise ChangeLedgerError("Submodule entries are not modified by checkpoints.")
            blob = self._git(["show", f"{commit_oid}:{rel_path}"], check=True).stdout

        displaced = self._displace_matching(
            target,
            rel_path=rel_path,
            expected_identity=expected_identity,
        )
        try:
            self._install_tree_entry_no_replace(target, identity=identity, blob=blob)
        except FileExistsError as exc:
            # A writer claimed the name after the verified version was moved
            # aside. Their content wins. The old agent version remains durable
            # in ``rollback_oid``, so the private duplicate can be removed.
            if displaced is not None:
                displaced.unlink(missing_ok=True)
            raise StaleWorkspaceError(
                "Workspace files changed after the checkpoint; refusing to overwrite newer work."
            ) from exc
        except Exception:
            if displaced is not None and not _publish_entry_no_replace(displaced, target):
                raise ChangeLedgerError(
                    f"Unable to restore {rel_path}; a recovery copy remains at {displaced.name}."
                ) from None
            raise

        if identity is None:
            _remove_empty_parents(target.parent, stop=self.workspace_root)
        return _RestoreMutation(
            rel_path=rel_path,
            before_identity=expected_identity,
            after_identity=identity,
            displaced_path=displaced,
        )

    def _displace_matching(
        self,
        target: Path,
        *,
        rel_path: str,
        expected_identity: tuple[str, str] | None,
    ) -> Path | None:
        if self._path_identity(target) != expected_identity:
            raise StaleWorkspaceError(
                "Workspace files changed after the checkpoint; refusing to overwrite newer work."
            )
        if expected_identity is None:
            return None

        displaced = _reserve_displaced_path(target)
        target_was_displaced = False
        try:
            try:
                os.replace(target, displaced)
                target_was_displaced = True
            except OSError as exc:
                raise StaleWorkspaceError(
                    "Workspace files changed after the checkpoint; refusing to overwrite newer work."
                ) from exc
            if self._path_identity(displaced) != expected_identity:
                if not _publish_entry_no_replace(displaced, target):
                    raise ChangeLedgerError(
                        f"A concurrent edit to {rel_path} was preserved at {displaced.name}."
                    )
                raise StaleWorkspaceError(
                    "Workspace files changed after the checkpoint; refusing to overwrite newer work."
                )
            return displaced
        finally:
            if not target_was_displaced:
                displaced.unlink(missing_ok=True)

    def _install_tree_entry_no_replace(
        self,
        target: Path,
        *,
        identity: tuple[str, str] | None,
        blob: bytes | None,
    ) -> None:
        if identity is None:
            if os.path.lexists(target):
                raise FileExistsError(os.fspath(target))
            return
        mode, _oid = identity
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "120000":
            assert blob is not None
            os.symlink(blob.decode("utf-8"), target)
            return
        if mode not in {"100644", "100755"}:
            raise ChangeLedgerError("Unsupported checkpoint entry type.")
        assert blob is not None
        permissions = 0o755 if mode == "100755" else 0o644
        staged = _stage_file(target, blob, mode=permissions)
        try:
            if not _publish_entry_no_replace(staged, target):
                raise FileExistsError(os.fspath(target))
        finally:
            staged.unlink(missing_ok=True)

    def _rollback_restore(self, mutation: _RestoreMutation) -> None:
        target = _safe_workspace_path(self.workspace_root, mutation.rel_path)
        discard = self._displace_matching(
            target,
            rel_path=mutation.rel_path,
            expected_identity=mutation.after_identity,
        )
        try:
            if mutation.displaced_path is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not _publish_entry_no_replace(mutation.displaced_path, target):
                    raise ChangeLedgerError(
                        f"Unable to roll back {mutation.rel_path}; newer content was preserved."
                    )
            elif os.path.lexists(target):
                raise StaleWorkspaceError(
                    "Workspace files changed during rollback; refusing to overwrite newer work."
                )
        finally:
            if discard is not None:
                discard.unlink(missing_ok=True)

    @staticmethod
    def _finalize_restore(mutation: _RestoreMutation) -> None:
        if mutation.displaced_path is not None:
            mutation.displaced_path.unlink(missing_ok=True)

    def _tree_identity(self, commit_oid: str, rel_path: str) -> tuple[str, str] | None:
        raw = self._git(["ls-tree", "-z", commit_oid, "--", rel_path], check=True).stdout
        if not raw:
            return None
        header, _separator, _path = raw.partition(b"\t")
        mode, _kind, oid = header.decode("ascii").split(" ", 2)
        return mode, oid

    def _worktree_identity(self, rel_path: str) -> tuple[str, str] | None:
        path = _safe_workspace_path(self.workspace_root, rel_path)
        return self._path_identity(path)

    def _blob_oid(self, data: bytes) -> str:
        # Equivalent to `git hash-object --stdin` (no path or filters). Baseline
        # capture checks every file twice; spawning Git for each hash adds
        # thousands of process launches in a normal repository, especially costly
        # on Windows. Keep the existing before/after stat and content checks.
        digest = hashlib.new(self._object_hash_name, usedforsecurity=False)
        digest.update(f"blob {len(data)}\0".encode("ascii"))
        digest.update(data)
        return digest.hexdigest()

    def _path_identity(self, path: Path) -> tuple[str, str] | None:
        if not os.path.lexists(path):
            return None
        if path.is_symlink():
            before = path.lstat()
            data = os.readlink(path).encode("utf-8")
            after = path.lstat()
            if _stat_fingerprint(before) != _stat_fingerprint(after):
                return "unstable", "unstable"
            return "120000", self._blob_oid(data)
        if not path.is_file():
            return "040000", "directory"
        try:
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                data = stream.read()
                after = os.fstat(stream.fileno())
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            return "unstable", "unstable"
        if _stat_fingerprint(before) != _stat_fingerprint(after):
            return "unstable", "unstable"
        executable = bool(after.st_mode & stat.S_IXUSR) and os.name != "nt"
        return ("100755" if executable else "100644"), self._blob_oid(data)

    def _session_git_env(self, session_id: str) -> dict[str, str]:
        env = build_git_process_env()
        env["GIT_INDEX_FILE"] = os.fspath(self.index_dir / f"{_session_key(session_id)}.index")
        return env

    def _git(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        input_bytes: bytes | None = None,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        process_env = dict(env or build_git_process_env())
        command = [
            "git",
            *(["-c", "core.longpaths=true"] if os.name == "nt" else []),
            "--git-dir=objects.git",
            f"--work-tree={os.fspath(self.workspace_root)}",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.filemode=false" if os.name == "nt" else "core.filemode=true",
            *args,
        ]
        try:
            result = subprocess.run(
                command,
                cwd=self.storage_dir,
                input=input_bytes,
                capture_output=True,
                timeout=self.git_timeout_seconds,
                check=False,
                env=process_env,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ChangeLedgerError("Checkpoint storage is unavailable.") from None
        if check and result.returncode != 0:
            raise ChangeLedgerError("Checkpoint Git operation failed.")
        return result

    def _head_locked(self, db: sqlite3.Connection, session_id: str) -> Checkpoint | None:
        row = db.execute(
            "SELECT c.* FROM session_heads h JOIN checkpoints c "
            "ON c.checkpoint_id = h.checkpoint_id WHERE h.session_id = ?",
            (session_id,),
        ).fetchone()
        return self._checkpoint_from_row(row) if row is not None else None

    def _checkpoint_locked(self, db: sqlite3.Connection, checkpoint_id: str) -> Checkpoint:
        clean = str(checkpoint_id or "").strip()
        row = db.execute("SELECT * FROM checkpoints WHERE checkpoint_id = ?", (clean,)).fetchone()
        if row is None:
            raise ChangeLedgerError("Checkpoint was not found.")
        return self._checkpoint_from_row(row)

    @staticmethod
    def _checkpoint_from_row(row: sqlite3.Row) -> Checkpoint:
        changes = tuple(ChangeRecord(**item) for item in json.loads(row["changes_json"]))
        return Checkpoint(
            checkpoint_id=row["checkpoint_id"],
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            step_id=row["step_id"],
            parent_id=row["parent_id"],
            commit_oid=row["commit_oid"],
            kind=row["kind"],
            created_at=row["created_at"],
            message=row["message"],
            changes=changes,
            omitted_paths=tuple(json.loads(row["omitted_json"])),
            non_revertible_paths=tuple(json.loads(row["non_revertible_json"])),
            reverts_id=row["reverts_id"],
            redoes_id=row["redoes_id"],
        )


def _validate_session_id(value: str) -> str:
    clean = str(value or "").strip()
    if not _SESSION_RE.fullmatch(clean):
        raise ChangeLedgerError("Invalid checkpoint session identifier.")
    return clean


def _bounded_label(value: str | None) -> str | None:
    clean = str(value or "").strip()
    return clean[:256] or None


def _bounded_message(value: str) -> str:
    return str(value or "").replace("\x00", "")[:1000]


def _session_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]


def _decode_git_path(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")


def _metadata_path_set(raw: object) -> set[str]:
    try:
        values = json.loads(str(raw)) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise ChangeLedgerError("Checkpoint omission metadata is invalid.") from exc
    if not isinstance(values, (list, tuple)):
        raise ChangeLedgerError("Checkpoint omission metadata is invalid.")
    result: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ChangeLedgerError("Checkpoint omission metadata is invalid.")
        if value == _ALL_PATHS_NON_REVERTIBLE:
            result.add(value)
            continue
        trailing_slash = value.replace("\\", "/").endswith("/")
        normalized = value.replace("\\", "/").strip("/")
        if (
            not normalized
            or normalized.startswith("../")
            or "/../" in f"/{normalized}/"
            or "\x00" in normalized
        ):
            raise ChangeLedgerError("Checkpoint omission metadata is invalid.")
        result.add(f"{normalized}/" if trailing_slash else normalized)
    return result


def _path_is_non_revertible(rel_path: str, protected: set[str]) -> bool:
    normalized = rel_path.replace("\\", "/").strip("/")
    if _ALL_PATHS_NON_REVERTIBLE in protected or normalized in protected:
        return True
    return any(
        item.endswith("/") and normalized.startswith(item)
        for item in protected
        if item != _ALL_PATHS_NON_REVERTIBLE
    )


def _is_runtime_or_sensitive(rel_path: str) -> bool:
    parts = tuple(part.lower() for part in Path(rel_path).parts)
    if not parts or parts[0] in {".git", ".alysis"}:
        return True
    # Keep the checkpoint boundary aligned with the filesystem tools' canonical
    # credential policy.  A narrower second list here previously allowed files
    # such as .npmrc, .netrc, cloud credentials, secret YAML, and keystores to
    # be copied into the external Git object database.
    return classify_sensitive_path(rel_path).sensitive


def _safe_workspace_path(root: Path, rel_path: str) -> Path:
    normalized = rel_path.replace("\\", "/").lstrip("/")
    unresolved = root / normalized
    # Resolve the parent chain to reject directory symlink escapes, but retain
    # the final component so a checkpointed symlink is inspected/restored as a
    # link rather than following it to its destination.
    candidate = unresolved.parent.resolve(strict=False) / unresolved.name
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ChangeLedgerError("Checkpoint path escapes the workspace.") from None
    return candidate


def _normalize_scope_paths(root: Path, paths: Iterable[object]) -> tuple[str, ...]:
    normalized_paths: set[str] = set()
    for raw in paths:
        value = str(raw or "").replace("\\", "/").strip()
        if not value:
            continue
        candidate_path = Path(value)
        if candidate_path.is_absolute():
            raise ChangeLedgerError("Checkpoint paths must be workspace-relative.")
        parts = tuple(part for part in value.split("/") if part not in {"", "."})
        if not parts or any(part == ".." for part in parts):
            raise ChangeLedgerError("Checkpoint paths must identify entries inside the workspace.")
        normalized = "/".join(parts)
        _safe_workspace_path(root, normalized)
        normalized_paths.add(normalized)
    return tuple(sorted(normalized_paths))


def _record_paths(records: tuple[ChangeRecord, ...]) -> tuple[str, ...]:
    paths: set[str] = set()
    for record in records:
        paths.add(record.path)
        if record.previous_path:
            paths.add(record.previous_path)
    return tuple(sorted(paths))


def _batches(paths: list[str], size: int = 128) -> list[list[str]]:
    return [paths[index : index + size] for index in range(0, len(paths), size)]


def _atomic_write(path: Path, data: bytes, *, mode: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _stage_file(target: Path, data: bytes, *, mode: int) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".staged", dir=target.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, mode)
        return temporary_path
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _reserve_displaced_path(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".displaced", dir=target.parent
    )
    os.close(handle)
    return Path(temporary)


def _publish_entry_no_replace(source: Path, target: Path) -> bool:
    """Publish ``source`` at an unclaimed name and consume it on success.

    ``False`` means only that another writer already owns ``target``. Other
    filesystem failures must remain distinguishable so restore callers keep
    the displaced recovery entry instead of treating an I/O failure as a
    harmless compare-and-swap miss.
    """

    try:
        info = source.lstat()
        if stat.S_ISREG(info.st_mode):
            os.link(source, target, follow_symlinks=False)
        elif stat.S_ISLNK(info.st_mode):
            os.symlink(os.readlink(source), target)
        else:
            raise ChangeLedgerError("Unsupported checkpoint recovery entry type.")
    except FileExistsError:
        return False
    source.unlink()
    return True


def _stat_fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(getattr(value, "st_dev", 0)),
        int(getattr(value, "st_ino", 0)),
        int(value.st_size),
        int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000))),
        int(getattr(value, "st_ctime_ns", int(value.st_ctime * 1_000_000_000))),
        int(value.st_mode),
    )


def _remove_empty_parents(path: Path, *, stop: Path) -> None:
    current = path
    while current != stop:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


__all__ = [
    "ChangeLedger",
    "ChangeLedgerError",
    "ChangeRecord",
    "Checkpoint",
    "StaleWorkspaceError",
]
