from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from alysis_code.ide import browser_profiles as profiles
from alysis_code.ide.browser_profiles import BrowserProfileError, BrowserProfileStore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX profile lease")


@pytest.fixture
def store(tmp_path):
    return BrowserProfileStore(tmp_path.resolve() / "private-profiles")


def metadata(lease):
    return lease.path.parent / "metadata.json"


def test_clean_reopen_preserves_storage_and_does_not_copy_tabs(store):
    lease = store.acquire("synthetic-account", "bot-α")
    assert lease.owner_id == "bot-α"
    path = lease.path
    path.joinpath("synthetic-storage").write_text("local value Ελληνικά")
    path.joinpath("Sessions").mkdir()
    path.joinpath("Sessions", "synthetic-tab").write_text("owned tab bytes")
    path.joinpath("DevToolsActivePort").write_text("12345\n/devtools/browser/old")
    assert lease.begin("bot-α") == path
    assert json.loads(metadata(lease).read_text())["state"] == "dirty"
    assert not path.joinpath("DevToolsActivePort").exists()
    assert path.joinpath("Sessions", "synthetic-tab").read_text() == "owned tab bytes"
    lease.mark_clean()
    lease.mark_clean()  # Other resources may require a cleanup retry.
    lease.release()
    lease.release()
    reopened = store.acquire("synthetic-account", "bot-α")
    try:
        assert reopened.path == path
        assert path.joinpath("synthetic-storage").read_text() == "local value Ελληνικά"
    finally:
        reopened.release()


def test_account_and_bot_identity_isolation(store):
    leases = [
        store.acquire(account, owner) for account, owner in (("a", "b"), ("a", "c"), ("d", "b"))
    ]
    try:
        assert len({lease.path for lease in leases}) == 3
        leases[0].path.joinpath("synthetic-storage").write_text("only first bot")
        assert all(not lease.path.joinpath("synthetic-storage").exists() for lease in leases[1:])
        for lease in leases:
            assert stat.S_IMODE(lease.path.stat().st_mode) == 0o700
            assert stat.S_IMODE(metadata(lease).stat().st_mode) == 0o600
            assert stat.S_IMODE((lease.path.parent / "lease.lock").stat().st_mode) == 0o600
    finally:
        for lease in leases:
            lease.release()


@pytest.mark.parametrize(
    "account,owner",
    [("", "bot"), ("a", " "), (None, "bot"), ("a", "x\0y"), ("a", "x" * 513), ("a", "\ud800")],
)
def test_invalid_identities_do_not_create_entries(store, account, owner):
    with pytest.raises(BrowserProfileError, match="identity"):
        store.acquire(account, owner)
    assert list(store.root.iterdir()) == []


def test_same_process_lease_exclusion_and_idle_release(store):
    lease = store.acquire("a", "b")
    with pytest.raises(BrowserProfileError, match="already in use"):
        BrowserProfileStore(store.root).acquire("a", "b")
    lease.release()
    another = store.acquire("a", "b")
    another.release()


def test_second_process_cannot_acquire_until_lock_is_released(store):
    script = """
import sys
from pathlib import Path
from alysis_code.ide.browser_profiles import BrowserProfileStore, BrowserProfileError
try:
    lease = BrowserProfileStore(Path(sys.argv[1])).acquire('account', 'bot')
except BrowserProfileError as exc:
    print(str(exc))
    sys.exit(3)
lease.release()
print('acquired')
"""
    lease = store.acquire("account", "bot")
    command = [sys.executable, "-c", script, str(store.root)]
    env = {"PATH": os.defpath, "PYTHONPATH": str(Path(profiles.__file__).resolve().parents[2])}
    try:
        blocked = subprocess.run(command, capture_output=True, text=True, timeout=5, env=env)
        assert blocked.returncode == 3
        assert blocked.stdout == "Browser profile is already in use.\n"
    finally:
        lease.release()
    acquired = subprocess.run(command, capture_output=True, text=True, timeout=5, env=env)
    assert acquired.returncode == 0, acquired.stderr
    assert acquired.stdout == "acquired\n"


def test_release_of_begun_profile_stays_dirty_and_preserves_data(store):
    lease = store.acquire("a", "b")
    lease.begin("b")
    saved = lease.path / "synthetic-storage"
    saved.write_text("do not erase")
    before = metadata(lease).read_bytes()
    lease.release()
    with pytest.raises(BrowserProfileError, match="dirty"):
        store.acquire("a", "b")
    assert metadata(lease).read_bytes() == before
    assert saved.read_text() == "do not erase"


@pytest.mark.parametrize("raw", [b"not json", b"{}", b"[]", b"x" * 5000])
def test_malformed_metadata_refused_without_repair(store, raw):
    lease = store.acquire("a", "b")
    marker = metadata(lease)
    lease.release()
    marker.write_bytes(raw)
    with pytest.raises(BrowserProfileError):
        store.acquire("a", "b")
    assert marker.read_bytes() == raw


@pytest.mark.parametrize(
    "field,value",
    [("owner", "other"), ("account", "other"), ("version", True), ("state", "recover")],
)
def test_metadata_identity_and_schema_are_checked(store, field, value):
    lease = store.acquire("a", "b")
    marker = metadata(lease)
    data = json.loads(marker.read_text())
    lease.release()
    data[field] = value
    marker.write_text(json.dumps(data))
    with pytest.raises(BrowserProfileError):
        store.acquire("a", "b")


def test_missing_metadata_is_not_treated_as_a_new_profile(store):
    lease = store.acquire("a", "b")
    marker = metadata(lease)
    lease.release()
    marker.unlink()
    with pytest.raises(BrowserProfileError):
        store.acquire("a", "b")
    assert not marker.exists()


def test_owner_duplicate_begin_and_released_lease_guards(store):
    lease = store.acquire("a", "b")
    with pytest.raises(BrowserProfileError, match="owned"):
        lease.begin("another-bot")
    with pytest.raises(BrowserProfileError, match="not begun"):
        lease.mark_clean()
    lease.begin("b")
    with pytest.raises(BrowserProfileError, match="already begun"):
        lease.begin("b")
    lease.mark_clean()
    with pytest.raises(BrowserProfileError, match="already begun"):
        lease.begin("b")
    lease.release()
    lease.release()
    with pytest.raises(BrowserProfileError, match="released"):
        lease.mark_clean()
    idle = store.acquire("a", "b")
    idle.release()
    with pytest.raises(BrowserProfileError, match="released"):
        idle.begin("b")


@pytest.mark.parametrize("kind", ["symlink", "directory", "hardlink", "fifo"])
def test_stale_endpoint_must_be_a_regular_exclusively_owned_file(store, tmp_path, kind):
    lease = store.acquire("a", "b")
    outside = tmp_path / "synthetic-outside"
    outside.write_text("untouched")
    endpoint = lease.path / "DevToolsActivePort"
    if kind == "symlink":
        endpoint.symlink_to(outside)
    elif kind == "directory":
        endpoint.mkdir()
    elif kind == "hardlink":
        os.link(outside, endpoint)
    else:
        os.mkfifo(endpoint)
    try:
        with pytest.raises(BrowserProfileError, match="endpoint metadata"):
            lease.begin("b")
        assert os.path.lexists(endpoint)
        assert outside.read_text() == "untouched"
    finally:
        lease.release()


def test_root_and_ancestor_symlinks_are_refused_without_creation(tmp_path):
    outside = tmp_path.resolve() / "outside"
    outside.mkdir(mode=0o700)
    link = tmp_path.resolve() / "linked"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(BrowserProfileError):
        BrowserProfileStore(link)
    with pytest.raises(BrowserProfileError):
        BrowserProfileStore(link / "new-root")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("target", ["root", "entry", "profile", "metadata", "lock"])
def test_nonprivate_storage_refused(store, target):
    lease = store.acquire("a", "b")
    path = {
        "root": store.root,
        "entry": lease.path.parent,
        "profile": lease.path,
        "metadata": metadata(lease),
        "lock": lease.path.parent / "lease.lock",
    }[target]
    lease.release()
    path.chmod(0o755 if path.is_dir() else 0o644)
    with pytest.raises(BrowserProfileError):
        store.acquire("a", "b")


@pytest.mark.parametrize("target", ["profile", "metadata", "lock"])
def test_symlink_replacement_while_leased_refused(store, tmp_path, target):
    lease = store.acquire("a", "b")
    path = {
        "profile": lease.path,
        "metadata": metadata(lease),
        "lock": lease.path.parent / "lease.lock",
    }[target]
    replacement = tmp_path / "synthetic-replacement"
    if path.is_dir():
        replacement.mkdir(mode=0o700)
    else:
        replacement.write_text("untouched")
        replacement.chmod(0o600)
    path.rename(path.with_name(path.name + ".saved"))
    path.symlink_to(replacement, target_is_directory=replacement.is_dir())
    try:
        with pytest.raises(BrowserProfileError):
            lease.begin("b")
        if replacement.is_file():
            assert replacement.read_text() == "untouched"
        else:
            assert list(replacement.iterdir()) == []
    finally:
        lease.release()


def test_begin_revalidates_metadata_after_acquire(store):
    lease = store.acquire("a", "b")
    value = json.loads(metadata(lease).read_text())
    value["state"] = "dirty"
    metadata(lease).write_text(json.dumps(value))
    try:
        with pytest.raises(BrowserProfileError, match="dirty"):
            lease.begin("b")
    finally:
        lease.release()


@pytest.mark.parametrize("operation", ["begin", "mark_clean"])
@pytest.mark.parametrize("failure", ["write", "fsync", "replace"])
def test_failed_atomic_transition_preserves_previous_metadata(
    store, monkeypatch, operation, failure
):
    lease = store.acquire("a", "b")
    if operation == "mark_clean":
        lease.begin("b")
    before = metadata(lease).read_bytes()

    def denied(*_args, **_kwargs):
        raise PermissionError("synthetic private storage diagnostic")

    with monkeypatch.context() as patch:
        patch.setattr(profiles.os, failure, denied)
        with pytest.raises(BrowserProfileError) as error:
            getattr(lease, operation)(*([] if operation == "mark_clean" else ["b"]))
        assert "private storage diagnostic" not in str(error.value)
    assert metadata(lease).read_bytes() == before
    assert not list(lease.path.parent.glob(".metadata-*.tmp"))
    if operation == "mark_clean":
        lease.mark_clean()  # Retry is allowed after a failed atomic replacement.
    else:
        with pytest.raises(BrowserProfileError, match="already begun"):
            lease.begin("b")
    lease.release()


def test_begin_fsyncs_dirty_metadata_before_removing_stale_endpoint_or_returning(
    store, monkeypatch
):
    lease = store.acquire("a", "b")
    endpoint = lease.path / "DevToolsActivePort"
    endpoint.write_text("stale")
    events = []
    fsync, replace, unlink = os.fsync, os.replace, os.unlink

    def record_fsync(fd):
        events.append("directory-fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file-fsync")
        return fsync(fd)

    def record_replace(*args, **kwargs):
        events.append("replace")
        return replace(*args, **kwargs)

    def record_unlink(name, **kwargs):
        if name == "DevToolsActivePort":
            assert json.loads(metadata(lease).read_text())["state"] == "dirty"
            events.append("endpoint-unlink")
        return unlink(name, **kwargs)

    monkeypatch.setattr(profiles.os, "fsync", record_fsync)
    monkeypatch.setattr(profiles.os, "replace", record_replace)
    monkeypatch.setattr(profiles.os, "unlink", record_unlink)
    lease.begin("b")
    assert events == [
        "file-fsync",
        "replace",
        "directory-fsync",
        "endpoint-unlink",
        "directory-fsync",
    ]
    lease.release()


@pytest.mark.parametrize("operation", ["begin", "mark_clean"])
def test_directory_fsync_failure_after_replace_never_reports_transition_success(
    store, monkeypatch, operation
):
    lease = store.acquire("a", "b")
    if operation == "mark_clean":
        lease.begin("b")
    original = os.fsync

    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic directory sync failure")
        return original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(profiles.os, "fsync", fail_directory)
        with pytest.raises(BrowserProfileError):
            getattr(lease, operation)(*([] if operation == "mark_clean" else ["b"]))
    if operation == "begin":
        assert json.loads(metadata(lease).read_text())["state"] == "dirty"
        with pytest.raises(BrowserProfileError, match="not begun"):
            lease.mark_clean()
        lease.release()
        with pytest.raises(BrowserProfileError, match="dirty"):
            store.acquire("a", "b")
    else:
        # The caller already attested a clean exit. Retry durably finishes the
        # successful replacement instead of treating it as foreign metadata.
        lease.mark_clean()
        lease.mark_clean()
        lease.release()
        reopened = store.acquire("a", "b")
        reopened.release()


def test_clean_metadata_changed_without_attestation_is_rejected(store):
    lease = store.acquire("a", "b")
    lease.begin("b")
    value = json.loads(metadata(lease).read_text())
    value["state"] = "clean"
    metadata(lease).write_text(json.dumps(value))
    try:
        with pytest.raises(BrowserProfileError, match="dirty state changed"):
            lease.mark_clean()
    finally:
        lease.release()


def test_recovery_requires_dead_group_in_same_process_namespace(store, monkeypatch):
    from alysis_code.ide import browser_profiles as module

    lease = store.acquire("a", "b")
    lease.begin("b")
    monkeypatch.setattr(module, "_process_context", lambda: {"boot": "boot", "namespace": "ns"})
    monkeypatch.setattr(module.os, "getpgid", lambda pid: pid)
    lease.record_process(12345)
    lease.release()
    monkeypatch.setattr(module.os, "killpg", lambda *args: None)
    with pytest.raises(BrowserProfileError, match="dirty"):
        store.acquire("a", "b")

    def dead(*args):
        raise ProcessLookupError()

    monkeypatch.setattr(module.os, "killpg", dead)
    monkeypatch.setattr(module, "_process_context", lambda: {"boot": "boot", "namespace": "other"})
    with pytest.raises(BrowserProfileError, match="dirty"):
        store.acquire("a", "b")
    monkeypatch.setattr(module, "_process_context", lambda: {"boot": "boot", "namespace": "ns"})
    recovered = store.acquire("a", "b")
    assert recovered.recovered
    recovered.begin("b")
    assert not (recovered.path.parent / "process.json").exists()
    recovered.release()
    with pytest.raises(BrowserProfileError, match="dirty"):
        store.acquire("a", "b")  # Crash before new process registration fails closed.
