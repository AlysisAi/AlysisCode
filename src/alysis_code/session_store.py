from __future__ import annotations

import getpass
import json
import os
import re
import socket
import stat
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import AppConfig, default_sessions_dir
from .logging_redaction import redact_log_text
from .managed_session import ManagedSessionError, ManagedSessionPersistence
from .session_artifacts import SessionArtifactLayout
from .web_research import SessionWebResearchTracker


def _now_ts() -> str:
    return datetime.now(UTC).isoformat()


def make_session_id() -> str:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    # 8 chars is enough for local uniqueness.
    import uuid

    return f"{ts}_{uuid.uuid4().hex[:8]}"


def sanitize_session_id(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", session_id.strip()) or "session"


def resolve_sessions_dir(cfg: AppConfig) -> Path:
    if cfg.session_log_dir:
        return Path(cfg.session_log_dir)
    return default_sessions_dir()


def local_session_owner() -> str | None:
    """Deterministic identity (``os-user@hostname``) of the local account.

    Session logs are stamped with their creator's identity so listing surfaces
    (``/resume``, "latest session" defaults) can hide conversations that were
    not created by this account — regardless of how a foreign log file arrived
    in the sessions directory (copied archive, baked disk image, shared host
    with distinct accounts). Purely local and deterministic: no network, no
    model/provider involvement. Returns ``None`` when neither component can be
    resolved; callers must treat that as "no local identity established".
    """
    try:
        user = getpass.getuser().strip()
    except (KeyError, OSError, ImportError):
        # No passwd entry / no LOGNAME-USER-USERNAME env; on py<=3.12 the
        # env-less fallback is a bare ``import pwd`` which raises
        # ModuleNotFoundError on Windows-family hosts (3.13+ maps it to
        # OSError) — this function must degrade to None, never crash startup.
        user = ""
    try:
        # Use the direct socket API instead of ``platform.node()``. On Windows,
        # ``platform.node()`` can invoke ``ver`` through a shell, which makes a
        # harmless identity lookup observable as an unrelated subprocess and
        # lets hook subprocess instrumentation intercept it.
        host = socket.gethostname().strip()
    except OSError:
        host = ""
    if not user and not host:
        return None
    return f"{user}@{host}"


class SessionStoreWriteError(OSError):
    """The session log did not accept a record; the event was not published.

    Raised by :meth:`SessionStore.append` when the store is durable and the
    JSONL write did not complete. The event is never visible through
    ``events_snapshot`` / ``events_since``. On disk the outcome is one of
    (:attr:`outcome`):

    * ``refused`` — nothing of the record remains authoritative: the file was
      truncated back, or an uncommitted-record boundary was recorded so a
      replay (:func:`read_session_events`) skips the bytes the attempt left;
    * ``indeterminate`` — the attempt may have left a complete-looking record
      on disk and the store could record neither the truncation nor a
      boundary. The store then refuses every further record until the
      boundary is recorded (:meth:`SessionStore.reconcile`); the persisted
      state of the session must not be trusted as complete until then.
    """

    def __init__(self, message: str, *, outcome: str = "refused") -> None:
        super().__init__(message)
        self.outcome = outcome


class SessionStoreUnavailableError(SessionStoreWriteError):
    """A durable session log was requested but cannot be opened.

    Raised by :class:`SessionStore` at construction when ``enabled=True`` and
    the sessions directory or the log file is unusable. Distinct from a
    deliberate ``enabled=False`` (``--no-log``): the caller asked for a
    durable record and did not get one, and must not run as if it had.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, outcome="unavailable")


# A record type the store writes *itself* (never through ``append``) right
# after a failed, non-truncatable write: everything between ``from_offset``
# and ``to_offset`` (or, when unknown, up to this record) was not committed and
# is skipped by :func:`read_session_events`. Also carried by the sidecar file
# ``<log>.uncommitted.json`` when the marker could not be written to the log.
SESSION_STORE_UNCOMMITTED_EVENT = "session_store_uncommitted"
_UNCOMMITTED_SIDECAR_SUFFIX = ".uncommitted.json"


@dataclass
class SessionInfo:
    session_id: str
    path: Path
    mtime: float
    # Log-derived scoping/recency metadata (all optional so any existing
    # ``SessionInfo(session_id=..., path=..., mtime=...)`` construction keeps
    # working). ``workspace_root``/``git_root`` come from the session log's own
    # events (not the filesystem) and let ``/resume`` show only the current
    # workspace's chats. ``last_event_ts`` is the log's own UTC ISO timestamp of
    # the most recent event, used for the displayed "when" instead of file mtime
    # (which drifts after copies/extracts).
    # ``owner`` is the log-recorded creator identity (see
    # :func:`local_session_owner`); listings hide sessions stamped by a
    # different account so one user's conversations never surface for another.
    # ``last_owner`` is the identity stamped on the log's newest event — the
    # account that most recently used the session. Matching either lets an
    # explicit ``/resume <id>`` re-adopt a session whose recorded identity
    # drifted (e.g. after a hostname rename): the resume appends fresh events
    # with the current identity, so the session self-heals into the listing.
    workspace_root: str | None = None
    git_root: str | None = None
    last_event_ts: str | None = None
    owner: str | None = None
    last_owner: str | None = None


class SessionStore:
    def __init__(
        self,
        *,
        enabled: bool,
        artifact_persistence_enabled: bool | None = None,
        sessions_dir: Path,
        session_id: str,
        cwd: str,
        repo_root: str | None,
        workspace_root: str | None = None,
        focus_dir: str | None = None,
        git_root: str | None = None,
        workspace_kind: str | None = None,
        binding_source: str | None = None,
        binding_requested_path: str | None = None,
        binding_risk_level: str | None = None,
        binding_created_path: bool | None = None,
        runtime_kind: str | None = None,
        active_workdir: str | None = None,
        active_workdir_relpath: str | None = None,
        owner: str | None = None,
        managed_session_persistence: ManagedSessionPersistence | None = None,
    ) -> None:
        self.enabled = enabled
        self.sessions_dir = sessions_dir
        self.session_id = session_id
        self.cwd = cwd
        self.repo_root = repo_root
        self.workspace_root = workspace_root
        # Stamp every log with its creator so listings can scope per account.
        self.owner = owner if owner is not None else local_session_owner()
        self.focus_dir = focus_dir
        self.git_root = git_root
        self.workspace_kind = workspace_kind
        self.binding_source = binding_source
        self.binding_requested_path = binding_requested_path
        self.binding_risk_level = binding_risk_level
        self.binding_created_path = binding_created_path
        self.runtime_kind = runtime_kind
        self.active_workdir = active_workdir or cwd
        self.active_workdir_relpath = active_workdir_relpath
        self._managed_session_persistence = managed_session_persistence
        if managed_session_persistence is not None:
            if managed_session_persistence.session_id != session_id:
                raise ValueError("Managed session id does not match SessionStore")
            # The root command already canonicalized and sealed this captured
            # path before provider setup.  Resolving it again here would
            # reintroduce a pathname race after the retained writer is final.
            if Path(sessions_dir) != managed_session_persistence.sessions_dir:
                raise ValueError("Managed session directory does not match SessionStore")
            self.sessions_dir = managed_session_persistence.sessions_dir
            self.path = managed_session_persistence.path
        else:
            self.path = sessions_dir / f"{session_id}.jsonl"
        self._lock = threading.RLock()
        self.artifact_persistence_enabled = (
            enabled if artifact_persistence_enabled is None else bool(artifact_persistence_enabled)
        )
        self._events: list[dict[str, Any]] = []
        self._transient_payload_redactor: Callable[[Any], Any] | None = None
        self._web_research = SessionWebResearchTracker()
        # An uncommitted-record boundary the store still owes the log: set
        # when a failed write could not be truncated away and the boundary
        # marker could not be written either. While pending, every append is
        # refused as ``indeterminate`` (see :meth:`reconcile`).
        self._pending_uncommitted: dict[str, Any] | None = None
        self._hydrate_existing_state()

        self._fh = None
        if self.enabled:
            if managed_session_persistence is not None:
                # The explicit App-managed path was admitted before provider
                # setup.  Never downgrade a failure at this security boundary
                # to best-effort logging.
                self._fh = managed_session_persistence.claim_text_writer()
            else:
                try:
                    self.sessions_dir.mkdir(parents=True, exist_ok=True)
                    fd = os.open(
                        self.path,
                        os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                    try:
                        metadata = os.fstat(fd)
                        if (
                            not stat.S_ISREG(metadata.st_mode)
                            or metadata.st_nlink != 1
                            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
                        ):
                            raise OSError("Session transcript must be an owned regular file")
                        if os.name == "posix":
                            os.fchmod(fd, 0o600)
                        self._fh = os.fdopen(fd, "a", encoding="utf-8")
                    except BaseException:
                        os.close(fd)
                        raise
                except OSError as exc:
                    raise SessionStoreUnavailableError(
                        f"session log {self.path} cannot be opened for writing: {exc}"
                    ) from exc
            self._adopt_uncommitted_sidecar()
            if self._pending_uncommitted is not None:
                self.close()
                raise SessionStoreUnavailableError(
                    f"session log {self.path} holds an uncommitted record that cannot be "
                    "marked as such (neither the log nor its sidecar is writable); the log "
                    "must be reconciled before it can be used"
                )

    @property
    def session_artifact_root(self) -> Path:
        return self.sessions_dir / sanitize_session_id(self.session_id)

    @property
    def session_artifact_layout(self) -> SessionArtifactLayout:
        return SessionArtifactLayout(filesystem_root=self.session_artifact_root)

    def runtime_artifact_path(self, *parts: str) -> Path:
        return self.session_artifact_layout.artifact_fs_path(*parts)

    def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        observation_payload: dict[str, Any] | None = None,
    ) -> str:
        """Record one event and return its ``event_id`` (``<session_id>:<n>``).

        The id lets host-owned state reference the originating event (for
        example the ``user_message`` a task objective came from).

        Publication is commit-consistent: for a durable store the record is
        written and flushed to the JSONL log *before* it becomes visible to
        ``events_snapshot`` / ``events_since``. A failed write raises
        :class:`SessionStoreWriteError` and publishes nothing, so host-owned
        state that treats a returned event id as "accepted" never diverges
        from what a later replay of the log will see. A store created with
        ``enabled=False`` (deliberate no-log operation) keeps events in memory
        only, as before.
        """

        with self._lock:
            payload = self._redact_transient_payload(payload)
            observation_payload = (
                self._redact_transient_payload(observation_payload)
                if observation_payload is not None
                else None
            )
            event = self._build_event(event_type=event_type, payload=payload)
            event_id = f"{self.session_id}:{len(self._events) + 1}"
            event["event_id"] = event_id
            if self.enabled:
                # An earlier failed write left disowned bytes whose in-log
                # boundary marker is still owed: it goes in first, so the
                # refusal stays authoritative however many records follow.
                if self._pending_uncommitted is not None and not self.reconcile():
                    raise SessionStoreWriteError(
                        f"session log {self.path} holds an uncommitted record whose boundary "
                        "marker cannot be written; refusing to record "
                        f"{event_type!r} until the log is reconciled",
                        outcome="indeterminate",
                    )
                self._write_record(event)
            self._events.append(event)
            observed_web_research = self._web_research.observe_event(
                event_type=event_type,
                payload=observation_payload if observation_payload is not None else payload,
                ts=str(event.get("ts") or "").strip() or None,
                event_id=str(event.get("event_id") or "").strip() or None,
            )
            if observed_web_research and self.artifact_persistence_enabled:
                self._persist_web_research_artifact()
        return event_id

    def _write_record(self, event: dict[str, Any]) -> None:
        """Append one record to the log; raise without publishing on failure.

        The record is complete on disk when this returns. When the write or
        flush fails, the handle is closed and the attempt is undone
        (:meth:`_discard_partial_record`): the file is truncated back to the
        length it had before the attempt or, when that is impossible, an
        uncommitted-record boundary is recorded so a replay never treats the
        bytes the attempt left as a committed record. The raised error says
        which outcome applies (``refused`` or ``indeterminate``). The handle is
        reopened on the next append so a transient failure does not silently
        disable logging.
        """

        line = redact_log_text(json.dumps(event, ensure_ascii=True) + "\n")
        fh = self._fh
        if fh is None:
            fh = self._reopen_log()
        if fh is None:
            raise SessionStoreWriteError(f"session log {self.path} is not writable")
        start = self._measure_log_end(fh)
        if start is None:
            # Without the pre-write length the attempt could not be undone
            # or even identified afterwards, so it is not made: nothing
            # reaches the file, the record is refused, and the handle is
            # dropped so the next append starts from a fresh one.
            self._close_log_handle()
            raise SessionStoreWriteError(
                f"could not measure session log {self.path} before writing "
                f"{event.get('type', 'event')!r}; the record was not attempted"
            )
        try:
            fh.write(line)
            fh.flush()
        except Exception as exc:
            outcome = self._discard_partial_record(start, event_type=str(event.get("type") or ""))
            detail = (
                ""
                if outcome == "refused"
                else "; the log may hold the uncommitted record and "
                "refuses further records until it can be marked (indeterminate)"
            )
            raise SessionStoreWriteError(
                f"could not write {event.get('type', 'event')!r} to session log "
                f"{self.path}: {exc}{detail}",
                outcome=outcome,
            ) from exc

    def _reopen_log(self) -> Any | None:
        try:
            if self._managed_session_persistence is not None:
                self._fh = self._managed_session_persistence.reopen_text_writer()
            else:
                self.sessions_dir.mkdir(parents=True, exist_ok=True)
                self._fh = self.path.open("a", encoding="utf-8")
        except (OSError, ManagedSessionError):
            self._fh = None
        return self._fh

    def _close_log_handle(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except Exception:  # noqa: BLE001 - the handle may already be failing
                pass

    def _measure_log_end(self, fh: Any) -> int | None:
        """The log's current length in bytes, or ``None`` when it cannot be known.

        Measured through the handle first, then through the path; the store
        holds its lock and is the only writer, so both describe the same
        end. The value is the only reliable identity of a record the store
        may have to disown later (:meth:`_discard_partial_record`).
        """

        if self._managed_session_persistence is not None:
            try:
                return self._managed_session_persistence.retained_log_size()
            except (OSError, ManagedSessionError):
                return None
        try:
            return int(os.fstat(fh.fileno()).st_size)
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        try:
            position = fh.tell()
            if isinstance(position, int) and position >= 0:
                return position
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        try:
            return int(self.path.stat().st_size) if self.path.exists() else 0
        except OSError:
            return None

    def _discard_partial_record(self, start: int, *, event_type: str = "") -> str:
        """Undo a failed write; returns ``refused`` or ``indeterminate``.

        First choice: truncate the file back to ``start`` (nothing of the
        attempt remains). When the file cannot be truncated, the bytes the
        attempt left may parse as a complete record, so the store records an
        uncommitted-record boundary ``[start, end)`` instead
        (:meth:`_record_uncommitted_boundary`) that :func:`read_session_events`
        honours for every record starting inside it, however many committed
        records follow later. Only when the boundary cannot be recorded at
        all is the outcome indeterminate, and the store then refuses further
        records until reconciled.
        """

        self._close_log_handle()
        end: int | None = None
        try:
            if self._managed_session_persistence is not None:
                end = self._managed_session_persistence.retained_log_size()
            else:
                end = self.path.stat().st_size if self.path.exists() else 0
        except (OSError, ManagedSessionError):
            end = None
        if end is not None and end <= start:
            # Nothing reached the file.
            return "refused"
        try:
            if self._managed_session_persistence is not None:
                self._managed_session_persistence.truncate_retained_log(start)
            else:
                os.truncate(self.path, start)
            return "refused"
        except (OSError, ManagedSessionError):
            pass
        boundary = {
            "from_offset": start,
            "to_offset": end,
            "event_type": event_type,
            "ts": _now_ts(),
        }
        recorded = self._record_uncommitted_boundary(boundary)
        if recorded == "marker":
            return "refused"
        # The marker is still owed to the log: until it is there, no further
        # record may follow the disowned bytes (see ``append``), so a
        # boundary whose end could not be measured stays "up to the marker"
        # and a sidecar's range stays "up to the end of the file".
        self._pending_uncommitted = boundary
        return "refused" if recorded == "sidecar" else "indeterminate"

    def _uncommitted_marker_line(self, boundary: dict[str, Any]) -> str:
        record = {
            "type": SESSION_STORE_UNCOMMITTED_EVENT,
            "ts": _now_ts(),
            "session_id": self.session_id,
            "payload": dict(boundary),
        }
        # A leading newline guarantees the marker starts its own line even
        # when the failed attempt left a partial line without a terminator.
        return "\n" + json.dumps(record, ensure_ascii=True) + "\n"

    def _write_uncommitted_marker(self, boundary: dict[str, Any]) -> bool:
        try:
            if self._managed_session_persistence is not None:
                self._managed_session_persistence.append_recovery_marker(
                    self._uncommitted_marker_line(boundary)
                )
            else:
                with self.path.open("a", encoding="utf-8") as marker_fh:
                    marker_fh.write(self._uncommitted_marker_line(boundary))
                    marker_fh.flush()
        except (OSError, ManagedSessionError):
            return False
        self._remove_uncommitted_sidecar()
        return True

    def _write_uncommitted_sidecar(self, boundary: dict[str, Any]) -> bool:
        # No sidecar was admitted as a managed capability. An unavailable
        # in-log marker therefore leaves an explicit indeterminate outcome;
        # it must never trigger a pathname-based fallback write.
        if self._managed_session_persistence is not None:
            return False
        try:
            self._uncommitted_sidecar_path.write_text(
                json.dumps(boundary, ensure_ascii=True), encoding="utf-8"
            )
        except OSError:
            return False
        return True

    def _record_uncommitted_boundary(self, boundary: dict[str, Any]) -> str:
        """Persist the boundary; returns ``marker``, ``sidecar`` or ``none``.

        The marker in the log is the reconciled form: replay reads it in
        place and it survives every later append. The sidecar file is the
        durable fallback for the next open (:meth:`_adopt_uncommitted_sidecar`)
        when the log itself cannot be appended to; it does not count as
        reconciled.
        """

        if self._write_uncommitted_marker(boundary):
            return "marker"
        if self._write_uncommitted_sidecar(boundary):
            return "sidecar"
        return "none"

    @property
    def _uncommitted_sidecar_path(self) -> Path:
        return self.path.with_name(self.path.name + _UNCOMMITTED_SIDECAR_SUFFIX)

    def _remove_uncommitted_sidecar(self) -> None:
        if self._managed_session_persistence is not None:
            return
        try:
            self._uncommitted_sidecar_path.unlink()
        except OSError:
            pass

    def _adopt_uncommitted_sidecar(self) -> None:
        """At open: fold a sidecar boundary left by an earlier process into the
        log as a marker (or keep it pending, refusing records, if that fails)."""

        if self._managed_session_persistence is not None:
            # Legacy sidecars are refused before managed admission. Later
            # pathname substitutions cannot become recovery authority.
            return
        boundary = _read_uncommitted_sidecar(self.path)
        if boundary is None:
            return
        if not self._write_uncommitted_marker(boundary):
            self._pending_uncommitted = boundary

    @property
    def integrity_uncertain(self) -> bool:
        """True while an uncommitted-record boundary is still owed to the log.

        The refusal itself may already be durable (sidecar written); what is
        owed is the in-log marker, and no record is appended before it.
        """

        return self._pending_uncommitted is not None

    def reconcile(self) -> bool:
        """Try again to write a pending uncommitted-record boundary marker.

        Returns True when the log is reconciled (the marker is in the log or
        none was pending) and the store accepts records again. When the
        marker still cannot be written, the sidecar is (re)written so the
        refusal at least survives to the next open.
        """

        with self._lock:
            boundary = self._pending_uncommitted
            if boundary is None:
                return True
            if self._write_uncommitted_marker(boundary):
                self._pending_uncommitted = None
                return True
            self._write_uncommitted_sidecar(boundary)
            return False

    def _build_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "type": event_type,
            "ts": _now_ts(),
            "session_id": self.session_id,
            "cwd": self.cwd,
            "repo_root": self.repo_root,
            "pid": os.getpid(),
            "payload": payload,
        }
        if self.workspace_root is not None:
            event["workspace_root"] = self.workspace_root
        if self.owner is not None:
            event["owner"] = self.owner
        if self.focus_dir is not None:
            event["focus_dir"] = self.focus_dir
        if self.git_root is not None:
            event["git_root"] = self.git_root
        if self.workspace_kind is not None:
            event["workspace_kind"] = self.workspace_kind
        if self.binding_source is not None:
            event["binding_source"] = self.binding_source
        if self.binding_requested_path is not None:
            event["binding_requested_path"] = self.binding_requested_path
        if self.binding_risk_level is not None:
            event["binding_risk_level"] = self.binding_risk_level
        if self.binding_created_path is not None:
            event["binding_created_path"] = self.binding_created_path
        if self.runtime_kind is not None:
            event["runtime_kind"] = self.runtime_kind
        if self.active_workdir is not None:
            event["active_workdir"] = self.active_workdir
        if self.active_workdir_relpath is not None:
            event["active_workdir_relpath"] = self.active_workdir_relpath
        return event

    def update_active_workdir(self, *, cwd: str, active_workdir_relpath: str) -> None:
        self.cwd = cwd
        self.active_workdir = cwd
        self.active_workdir_relpath = active_workdir_relpath

    def set_transient_payload_redactor(
        self,
        redactor: Callable[[Any], Any] | None,
    ) -> None:
        """Install a turn-scoped redactor for events and store-owned artifacts."""

        with self._lock:
            self._transient_payload_redactor = redactor

    def _redact_transient_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        redactor = self._transient_payload_redactor
        if redactor is None:
            return payload
        redacted = redactor(payload)
        return redacted if isinstance(redacted, dict) else {}

    def append_artifact_jsonl(self, *parts: str, payload: dict[str, Any]) -> Path | None:
        with self._lock:
            if not self.enabled:
                return None
            payload = self._redact_transient_payload(payload)
            path = self.runtime_artifact_path(*parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(redact_log_text(json.dumps(payload, ensure_ascii=True) + "\n"))
            return path

    def events_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._events, ensure_ascii=True))

    def events_since(self, cursor: int) -> tuple[list[dict[str, Any]], int]:
        """Return events appended at or after ``cursor`` and the next cursor.

        Events are append-only and their positional order is protected by the
        store lock. A slice is therefore enough for incremental consumers; in
        particular, this avoids the full JSON round-trip used by
        :meth:`events_snapshot`.
        """
        with self._lock:
            start = max(0, int(cursor))
            next_cursor = len(self._events)
            return self._events[start:], next_cursor

    def classify_web_fetch_url(self, raw_url: Any) -> str | None:
        return self._web_research.classify_fetch_url(raw_url)

    def resolve_web_fetch_url(self, raw_url: Any) -> tuple[str | None, str | None]:
        return self._web_research.resolve_fetch_url(raw_url)

    def fetchable_web_fetch_urls(self, *, limit: int = 10) -> list[str]:
        return self._web_research.fetchable_urls(limit=limit)

    def configure_web_fetch_trusted_domains(self, domains: Iterable[Any] | None) -> tuple[str, ...]:
        """Install the configured trusted-domain allowlist for web_fetch.

        Session configuration rather than observed evidence: idempotent, never
        persisted into the web-research artifact, and it survives artifact
        hydration on session resume.
        """
        with self._lock:
            return self._web_research.configure_trusted_domains(domains)

    def establish_search_mediated_web_fetch_url(
        self,
        *,
        raw_url: Any,
        query: str,
        source_url: str | None = None,
    ) -> tuple[bool, str | None]:
        with self._lock:
            changed, normalized = self._web_research.establish_search_mediated_fetch_url(
                raw_url=raw_url,
                query=query,
                source_url=source_url,
            )
            if changed and self.artifact_persistence_enabled:
                self._persist_web_research_artifact()
            return changed, normalized

    def web_research_artifact_payload(self) -> dict[str, Any]:
        return self._web_research.artifact_payload()

    def web_research_metrics_payload(self) -> dict[str, int]:
        return self._web_research.metrics_payload()

    def has_web_research_activity(self) -> bool:
        return self._web_research.has_activity()

    def _persist_web_research_artifact(self) -> Path | None:
        if not self.artifact_persistence_enabled or not self._web_research.has_activity():
            return None
        path = self.runtime_artifact_path("web_research_sources.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            redact_log_text(
                json.dumps(
                    self._web_research.artifact_payload(),
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=True,
                )
                + "\n"
            ),
            encoding="utf-8",
        )
        return path

    def _hydrate_existing_state(self) -> None:
        self._hydrate_from_existing_log()
        self._hydrate_from_existing_web_research_artifact()
        self._web_research.clear_pending()

    def _hydrate_from_existing_log(self) -> bool:
        if self._managed_session_persistence is not None:
            events = self._managed_session_persistence.events_snapshot()
            if not events:
                return False
            self._events = events
            for index, event in enumerate(self._events, start=1):
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                self._web_research.observe_event(
                    event_type=str(event.get("type") or "").strip(),
                    payload=payload,
                    ts=str(event.get("ts") or "").strip() or None,
                    event_id=(
                        str(event.get("event_id") or "").strip() or f"{self.session_id}:{index}"
                    ),
                )
            return True
        try:
            if not self.path.exists():
                return False
            events = list(read_session_events(self.path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not events:
            return False
        self._events = json.loads(json.dumps(events, ensure_ascii=True))
        for index, event in enumerate(self._events, start=1):
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            self._web_research.observe_event(
                event_type=str(event.get("type") or "").strip(),
                payload=payload,
                ts=str(event.get("ts") or "").strip() or None,
                event_id=str(event.get("event_id") or "").strip() or f"{self.session_id}:{index}",
            )
        return True

    def _hydrate_from_existing_web_research_artifact(self) -> bool:
        artifact_path = self.runtime_artifact_path("web_research_sources.json")
        try:
            if not artifact_path.exists():
                return False
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        return self._web_research.merge_from_artifact_payload(payload)

    def close(self) -> None:
        if self.artifact_persistence_enabled and self._web_research.has_activity():
            self._persist_web_research_artifact()
        if self._pending_uncommitted is not None:
            # Last chance to leave the boundary on disk for the next process.
            self.reconcile()
        if self._fh:
            self._fh.close()
            self._fh = None


def _nonempty_str(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def canonical_workspace_path(raw: str | Path | None) -> Path | None:
    """Canonicalize a workspace/git-root path for identity comparison.

    Uses ``Path.resolve()`` so trailing slashes, ``.``/``..`` segments, and the
    OS's own case rules are absorbed (case-insensitive on Windows via the
    resolved canonical casing, case-sensitive on POSIX) without any per-platform
    special-casing. Returns ``None`` for empty/unresolvable input. This is
    deterministic and provider/model-agnostic by construction.
    """
    text = _nonempty_str(raw if isinstance(raw, str) else (str(raw) if raw is not None else None))
    if text is None:
        return None
    try:
        return Path(text).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def session_belongs_to_workspace(
    info: SessionInfo,
    current_workspace_root: Path | None,
    current_git_root: Path | None = None,
) -> bool:
    """Return True if ``info`` (a session log) belongs to the current workspace.

    Primary identity is the log's recorded ``workspace_root``. When that is
    absent (a legacy log predating workspace_root recording) we fall back to
    ``git_root``. When neither yields a deterministic match we return False:
    such strays are hidden from the scoped picker but remain reachable by an
    explicit ``/resume <id>``. ``current_workspace_root``/``current_git_root``
    must already be canonicalized via :func:`canonical_workspace_path`.
    """
    if info.workspace_root is not None:
        if current_workspace_root is None:
            return False
        session_ws = canonical_workspace_path(info.workspace_root)
        return session_ws is not None and session_ws == current_workspace_root
    if info.git_root is not None and current_git_root is not None:
        session_git = canonical_workspace_path(info.git_root)
        return session_git is not None and session_git == current_git_root
    return False


def session_belongs_to_owner(info: SessionInfo, current_owner: str | None) -> bool:
    """Return True if ``info`` (a session log) may be listed for this account.

    A log stamped with an owner is only listed when the current identity
    matches either the creator stamp (``owner``, from the log's first events)
    or the newest event's stamp (``last_owner``) — compared case-insensitively,
    since Windows usernames and DNS hostnames are not case-significant. The
    ``last_owner`` leg is the self-heal path: when a user's identity drifts
    (hostname rename, WSL vs native), one explicit ``/resume <id>`` appends
    fresh events under the new identity and the session lists again. A foreign
    log stays hidden either way — both its stamps are foreign.

    Legacy logs with no recorded owner on either end stay visible so an
    upgrade never empties a user's own list (workspace scoping still applies
    to them). When the log records an owner but the local identity cannot be
    established, the log is hidden: a foreign-stamped conversation must never
    surface on an unidentifiable account. Hidden logs remain reachable by an
    explicit ``/resume <id>`` on the machine that holds them.
    """
    recorded_first = _nonempty_str(info.owner)
    recorded_last = _nonempty_str(info.last_owner)
    if recorded_first is None and recorded_last is None:
        return True
    current = _nonempty_str(current_owner)
    if current is None:
        return False
    current_folded = current.casefold()
    if recorded_first is not None and recorded_first.casefold() == current_folded:
        return True
    return recorded_last is not None and recorded_last.casefold() == current_folded


def filter_sessions_to_local_owner(infos: list[SessionInfo]) -> list[SessionInfo]:
    """Drop sessions stamped by a different account than the local one."""
    current_owner = local_session_owner()
    return [info for info in infos if session_belongs_to_owner(info, current_owner)]


def read_session_first_event_workspace(path: Path) -> tuple[str | None, str | None]:
    """Read the session log's recorded (workspace_root, git_root)."""
    workspace_root, git_root, _owner = read_session_first_event_scope(path)
    return workspace_root, git_root


def read_session_first_event_scope(path: Path) -> tuple[str | None, str | None, str | None]:
    """Read the log's recorded (workspace_root, git_root, owner).

    Every event carries these at top level (see :meth:`SessionStore._build_event`)
    and the ``session_start`` payload also records the workspace fields; we scan
    the first few events and return the first non-empty values found. Bounded so
    we never read a whole (possibly large) log. Any read/parse error yields
    ``(None, None, None)``.
    """
    workspace_root: str | None = None
    git_root: str | None = None
    owner: str | None = None
    events = read_session_events(path)
    try:
        checked = 0
        for event in events:
            if not isinstance(event, dict):
                continue
            checked += 1
            if workspace_root is None:
                workspace_root = _nonempty_str(event.get("workspace_root"))
            if git_root is None:
                git_root = _nonempty_str(event.get("git_root"))
            if owner is None:
                owner = _nonempty_str(event.get("owner"))
            payload = event.get("payload")
            if isinstance(payload, dict):
                if workspace_root is None:
                    workspace_root = _nonempty_str(payload.get("workspace_root"))
                if git_root is None:
                    git_root = _nonempty_str(payload.get("git_root"))
                if owner is None:
                    owner = _nonempty_str(payload.get("owner"))
            # Keep scanning (within the bound) until the owner stamp is found
            # too: a pre-upgrade log resumed post-upgrade carries its owner
            # only on later events, and classifying it as legacy would leave
            # it visible to every account.
            if workspace_root is not None and git_root is not None and owner is not None:
                break
            if checked >= 5:
                break
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None, None
    finally:
        close = getattr(events, "close", None)
        if callable(close):
            close()
    return workspace_root, git_root, owner


def read_session_last_event_ts(path: Path) -> str | None:
    """Return the UTC ISO ``ts`` of the log's most recent event."""
    ts, _owner = read_session_last_event_fields(path)
    return ts


def read_session_last_event_fields(path: Path) -> tuple[str | None, str | None]:
    """Return the (``ts``, ``owner``) recorded at the tail of the log.

    Tail-scans the last chunk of the file (events are append-only and flushed,
    so the final parseable line is the newest event) rather than parsing the
    whole log. ``ts`` comes from the newest event that carries one; ``owner``
    comes from the newest parseable event — the account that most recently
    used the session. Returns ``(None, None)`` on empty/corrupt/unreadable
    logs so callers fall back to file mtime / creator-stamp semantics.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            file_size = fh.tell()
            if file_size <= 0:
                return None, None
            read_size = min(file_size, 16384)
            fh.seek(file_size - read_size)
            data = fh.read()
    except OSError:
        return None, None
    owner: str | None = None
    seen_event = False
    for raw_line in reversed(data.split(b"\n")):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict):
            if not seen_event:
                owner = _nonempty_str(obj.get("owner"))
                seen_event = True
            ts = _nonempty_str(obj.get("ts"))
            if ts is not None:
                return ts, owner
    return None, owner


def _epoch_from_iso(raw: str | None) -> float | None:
    text = _nonempty_str(raw)
    if text is None:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    try:
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return None


def _session_recency_sort_key(info: SessionInfo) -> float:
    # Prefer the log's own last-event timestamp (immune to copy/extract mtime
    # resets); fall back to filesystem mtime. Both are absolute epoch seconds,
    # so they order correctly even when mixed.
    epoch = _epoch_from_iso(info.last_event_ts)
    if epoch is not None:
        return epoch
    try:
        return float(info.mtime)
    except (TypeError, ValueError):
        return 0.0


def list_sessions(sessions_dir: Path) -> list[SessionInfo]:
    if not sessions_dir.exists():
        return []
    out: list[SessionInfo] = []
    for p in sessions_dir.glob("*.jsonl"):
        try:
            st = p.stat()
        except OSError:
            continue
        try:
            workspace_root, git_root, owner = read_session_first_event_scope(p)
        except OSError:
            workspace_root, git_root, owner = None, None, None
        try:
            last_event_ts, last_owner = read_session_last_event_fields(p)
        except OSError:
            last_event_ts, last_owner = None, None
        out.append(
            SessionInfo(
                session_id=p.stem,
                path=p,
                mtime=st.st_mtime,
                workspace_root=workspace_root,
                git_root=git_root,
                last_event_ts=last_event_ts,
                owner=owner,
                last_owner=last_owner,
            )
        )
    out.sort(key=_session_recency_sort_key, reverse=True)
    return out


def _read_uncommitted_sidecar(path: Path) -> dict[str, Any] | None:
    sidecar = path.with_name(path.name + _UNCOMMITTED_SIDECAR_SUFFIX)
    try:
        if not sidecar.exists():
            return None
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _offset_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass(frozen=True)
class _UncommittedRange:
    """Bytes a boundary disowns: records starting in ``[start, end)``.

    ``start`` is ``None`` when the boundary could not say where the failed
    attempt began (a record from an earlier build; the store no longer
    attempts a write it cannot locate). Such a boundary cannot identify the
    disowned bytes and is reported to consumers instead of applied.
    """

    start: int | None
    end: int
    source: str  # marker | sidecar


def _iter_log_lines(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    """``(byte_offset, record)`` for every parseable JSON object line."""

    offset = 0
    with path.open("rb") as fh:
        for raw in fh:
            record_offset = offset
            offset += len(raw)
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield record_offset, obj


def _uncommitted_ranges(path: Path) -> list[_UncommittedRange]:
    """Every boundary the log (markers) and its sidecar declare."""

    ranges: list[_UncommittedRange] = []
    marker_starts: set[int | None] = set()
    log_end = 0
    for record_offset, obj in _iter_log_lines(path):
        log_end = max(log_end, record_offset + 1)
        if str(obj.get("type") or "") != SESSION_STORE_UNCOMMITTED_EVENT:
            continue
        boundary = obj.get("payload")
        boundary = boundary if isinstance(boundary, dict) else {}
        start = _offset_or_none(boundary.get("from_offset"))
        end = _offset_or_none(boundary.get("to_offset"))
        # A marker is written right after the failed attempt, before any
        # other record, so "up to the marker" is exact when the end could
        # not be measured at the time.
        ranges.append(
            _UncommittedRange(
                start=start,
                end=record_offset if end is None else min(end, record_offset),
                source="marker",
            )
        )
        marker_starts.add(start)
    sidecar = _read_uncommitted_sidecar(path)
    if sidecar is not None:
        start = _offset_or_none(sidecar.get("from_offset"))
        if start not in marker_starts:
            end = _offset_or_none(sidecar.get("to_offset"))
            if end is None:
                # No record is appended after a sidecar-only refusal until the
                # marker is in the log, so the disowned bytes run to the end.
                try:
                    end = int(path.stat().st_size)
                except OSError:
                    end = log_end
            ranges.append(_UncommittedRange(start=start, end=end, source="sidecar"))
    return ranges


def read_session_events(path: Path) -> Iterable[dict[str, Any]]:
    """Replay a session log's committed records in order.

    Malformed and blank lines are skipped. Every uncommitted-record boundary
    the log declares — a ``session_store_uncommitted`` marker written by the
    store after a failed write it could not truncate away, or the sidecar
    ``<log>.uncommitted.json`` holding the marker it could not write into the
    log — is applied by byte range: a record whose bytes start inside
    ``[from_offset, to_offset)`` is not yielded, however many committed
    records follow it and whether or not the store was reopened since.
    Markers themselves are not yielded. Replay therefore agrees with what the
    store published in memory: a refused record is never read back as
    authoritative.

    A boundary that cannot say where the failed attempt began (no
    ``from_offset``; only records from an earlier build can carry one) is not
    applied to a guessed record: it is yielded as a
    ``session_store_uncommitted`` event with ``payload.unidentified`` set, so
    consumers that need a trustworthy history (task recovery) can refuse
    explicitly instead of adopting or discarding the wrong record.
    """

    ranges = _uncommitted_ranges(path)
    identified: list[tuple[int, int]] = [
        (item.start, item.end) for item in ranges if item.start is not None
    ]
    unidentified = [item for item in ranges if item.start is None]
    for record_offset, obj in _iter_log_lines(path):
        if str(obj.get("type") or "") == SESSION_STORE_UNCOMMITTED_EVENT:
            payload = obj.get("payload")
            if isinstance(payload, dict) and _offset_or_none(payload.get("from_offset")) is None:
                yield {**obj, "payload": {**payload, "unidentified": True}}
            continue
        if any(start <= record_offset < end for start, end in identified):
            continue
        yield obj
    for item in unidentified:
        if item.source == "sidecar":
            yield {
                "type": SESSION_STORE_UNCOMMITTED_EVENT,
                "payload": {
                    "from_offset": None,
                    "to_offset": item.end,
                    "source": "sidecar",
                    "unidentified": True,
                },
            }
