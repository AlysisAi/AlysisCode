from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from alysis_code.managed_session import ManagedSessionError, prepare_managed_session_persistence
from alysis_code.session_store import (
    SESSION_STORE_UNCOMMITTED_EVENT,
    SessionStore,
    SessionStoreWriteError,
    read_session_events,
)


class _FailedFlush:
    def __init__(self, writer):
        self.writer = writer

    def fileno(self):
        return self.writer.fileno()

    def write(self, text):
        return self.writer.write(text)

    def flush(self):
        self.writer.flush()
        raise OSError("synthetic failure after complete write")

    def close(self):
        self.writer.close()


def _store(tmp_path):
    capability = prepare_managed_session_persistence(
        sessions_dir=tmp_path / "sessions", session_id="managed"
    )
    capability.seal()
    store = SessionStore(
        enabled=True,
        sessions_dir=capability.sessions_dir,
        session_id=capability.session_id,
        cwd=str(tmp_path),
        repo_root=None,
        managed_session_persistence=capability,
    )
    return store, capability


def _deny_truncate(_size):
    raise OSError("append-only truncation refused")


@pytest.mark.parametrize("force_marker", [False, True])
def test_failed_writer_recovery_never_targets_a_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force_marker: bool
):
    store, capability = _store(tmp_path)
    original = store.path
    detached = original.with_name("detached.jsonl")
    canary = b"replacement must remain unchanged\n"
    swapped = False
    real_close = store._close_log_handle

    def close_then_replace():
        nonlocal swapped
        real_close()
        try:
            original.rename(detached)
        except PermissionError as exc:
            # Windows's retained data handle prevents this replacement. POSIX
            # allows it; the duplicate must still address the detached file.
            if os.name != "nt" or exc.winerror not in (5, 32):
                raise
        else:
            swapped = True
            original.write_bytes(canary)

    try:
        store.append("committed", {"content": "before"})
        if force_marker:
            monkeypatch.setattr(capability, "truncate_retained_log", _deny_truncate)
        monkeypatch.setattr(store, "_close_log_handle", close_then_replace)
        store._fh = _FailedFlush(store._fh)
        with pytest.raises(SessionStoreWriteError) as error:
            store.append("refused", {"content": "private payload"})
        assert error.value.outcome == "refused"
        store.append("committed", {"content": "after"})
        assert not store.integrity_uncertain
    finally:
        store.close()
        capability.close()
    if swapped:
        assert original.read_bytes() == canary
    replay = list(read_session_events(detached if swapped else original))
    assert [event["payload"]["content"] for event in replay] == ["before", "after"]
    # Managed replay must apply the same marker ranges after reopening.
    if swapped:
        original.unlink()
        detached.rename(original)
    resumed = prepare_managed_session_persistence(
        sessions_dir=original.parent, session_id="managed", append=False
    )
    try:
        assert resumed.events_snapshot() == replay
    finally:
        resumed.close()


def test_retry_uses_capability_even_if_store_path_metadata_changes(tmp_path: Path):
    store, capability = _store(tmp_path)
    original = store.path
    foreign = tmp_path / "foreign.jsonl"
    foreign.write_text("foreign\n", encoding="utf-8")
    try:
        store.append("before", {})
        store._close_log_handle()
        store.path = foreign
        store.append("after", {"content": "private"})
    finally:
        store.close()
        capability.close()
    assert foreign.read_text(encoding="utf-8") == "foreign\n"
    assert [event["type"] for event in read_session_events(original)] == ["before", "after"]


def test_unavailable_recovery_capability_never_falls_back_to_path(tmp_path: Path):
    store, capability = _store(tmp_path)
    original = store.path
    try:
        store.append("before", {})
        store._close_log_handle()
        capability.close()
        before = original.read_bytes()
        with pytest.raises(SessionStoreWriteError, match="not writable"):
            store.append("must-not-appear", {})
        assert original.read_bytes() == before
    finally:
        store.close()
        capability.close()


def test_marker_failure_is_indeterminate_without_path_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, capability = _store(tmp_path)
    original = store.path
    marker_writer = capability.append_recovery_marker
    try:
        store.append("before", {})
        monkeypatch.setattr(capability, "truncate_retained_log", _deny_truncate)
        monkeypatch.setattr(capability, "append_recovery_marker", _deny_truncate)
        store._fh = _FailedFlush(store._fh)
        with pytest.raises(SessionStoreWriteError) as error:
            store.append("refused", {})
        assert error.value.outcome == "indeterminate"
        assert store.integrity_uncertain
        assert not store._uncommitted_sidecar_path.exists()
        with pytest.raises(SessionStoreWriteError):
            store.append("blocked", {})
        monkeypatch.setattr(capability, "append_recovery_marker", marker_writer)
        assert store.reconcile()
        store.append("after", {})
    finally:
        store.close()
        capability.close()
    assert [event["type"] for event in read_session_events(original)] == ["before", "after"]


def test_existing_legacy_sidecar_refuses_managed_admission_without_mutation(tmp_path: Path):
    log = tmp_path / "managed.jsonl"
    log.write_bytes(b'{"type":"before"}\n')
    sidecar = tmp_path / "managed.jsonl.uncommitted.json"
    content = b'{"from_offset":0,"to_offset":19}'
    sidecar.write_bytes(content)
    with pytest.raises(ManagedSessionError, match="legacy uncommitted sidecar"):
        prepare_managed_session_persistence(sessions_dir=tmp_path, session_id="managed")
    assert log.read_bytes() == b'{"type":"before"}\n'
    assert sidecar.read_bytes() == content


def test_late_sidecar_cannot_be_adopted_or_removed_through_a_path(tmp_path: Path):
    store, capability = _store(tmp_path)
    sidecar = store._uncommitted_sidecar_path
    content = b'{"from_offset":0,"to_offset":999}'
    sidecar.write_bytes(content)
    try:
        store._adopt_uncommitted_sidecar()
        store._remove_uncommitted_sidecar()
        assert not store._write_uncommitted_sidecar({"from_offset": 1})
        assert not store.integrity_uncertain
        assert sidecar.read_bytes() == content
    finally:
        store.close()
        capability.close()


def test_managed_replay_preserves_unidentified_boundary_as_explicit_evidence(tmp_path: Path):
    events = [
        {"type": "before"},
        {"type": SESSION_STORE_UNCOMMITTED_EVENT, "payload": {"from_offset": None}},
        {"type": "after"},
    ]
    path = tmp_path / "managed.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    capability = prepare_managed_session_persistence(
        sessions_dir=tmp_path, session_id="managed", append=False
    )
    try:
        assert capability.events_snapshot() == list(read_session_events(path))
        assert capability.events_snapshot()[1]["payload"]["unidentified"] is True
    finally:
        capability.close()


def test_recovery_duplicate_is_closed_with_capability(tmp_path: Path):
    store, capability = _store(tmp_path)
    retained = capability._recovery_fd
    assert retained is not None and not os.get_inheritable(retained)
    store.close()
    capability.close()
    with pytest.raises(OSError):
        os.fstat(retained)
