"""Profile lifecycle fixtures; real Chromium evidence is a separate Linux study."""

from dataclasses import replace

import pytest
from test_ide_managed_browser import make_service

from alysis_code.ide import managed_browser as module
from alysis_code.ide.browser_profiles import BrowserProfileError, BrowserProfileStore
from alysis_code.ide.managed_browser import (
    BrowserLaunchError,
    BrowserLimitError,
    BrowserOwnershipError,
)


def rig(tmp_path, monkeypatch, *, clean=True, group_stopped=True):
    service, executable, launcher, terminator, factory = make_service(tmp_path)
    service._config = replace(service._config, shutdown_grace_seconds=0.05)
    lease = BrowserProfileStore(tmp_path / "durable").acquire("account", "bot")
    original_call = factory.transport.call

    def call(method, *args, **kwargs):
        if method == "Browser.close" and clean:
            launcher.processes[-1].returncode = 0
        return original_call(method, *args, **kwargs)

    factory.transport.call = call
    monkeypatch.setattr(
        module,
        "_profile_process_group_stopped",
        lambda process: process.poll() is not None and group_stopped,
    )
    # A real terminator leaves an already-exited leader unchanged.
    original_terminate = terminator.terminate
    terminator.terminate = lambda process, **kwargs: (
        original_terminate(process, **kwargs) if process.poll() is None else None
    )
    session = service.start("bot", executable_path=executable, profile_lease=lease)
    return service, session, lease, launcher, factory


@pytest.mark.parametrize("close_all", [False, True])
def test_clean_close_retains_owned_storage_and_requires_fresh_session(
    tmp_path, monkeypatch, close_all
):
    service, session, lease, launcher, factory = rig(tmp_path, monkeypatch)
    path = lease.path
    (path / "synthetic-state").write_text("Aster Ω 日本語")
    assert launcher.specs[-1].profile_dir == path
    assert not (path / ".alysis-owned").exists()
    if close_all:
        service.close_all()
    else:
        service.close("bot", session.session_id)
    assert factory.transport.calls[-1][0] == "Browser.close"
    assert (path / "synthetic-state").read_text() == "Aster Ω 日本語"
    lease.release()
    restored = BrowserProfileStore(tmp_path / "durable").acquire("account", "bot")
    assert restored.begin("bot") == path
    assert not (path / "DevToolsActivePort").exists()
    restored.release()


@pytest.mark.parametrize("close_all", [False, True])
def test_forced_exit_retains_dirty_profile_without_claiming_saved(tmp_path, monkeypatch, close_all):
    service, session, lease, launcher, _ = rig(tmp_path, monkeypatch, clean=False)
    service.close_all() if close_all else service.close("bot", session.session_id)
    assert launcher.processes[-1].poll() == -9
    assert lease.path.exists()
    service.close_all()  # Cleanup retries cannot change the dirty state.
    lease.release()
    recovered = BrowserProfileStore(tmp_path / "durable").acquire("account", "bot")
    assert recovered.recovered
    recovered.release()


def test_live_descendants_keep_session_and_lease_dirty(tmp_path, monkeypatch):
    service, session, lease, _, _ = rig(tmp_path, monkeypatch, group_stopped=False)
    with pytest.raises(BrowserLaunchError, match="cleanup"):
        service.close("bot", session.session_id)
    assert session.session_id in service._sessions
    with pytest.raises(BrowserProfileError):
        BrowserProfileStore(tmp_path / "durable").acquire("account", "bot")
    monkeypatch.setattr(module, "_profile_process_group_stopped", lambda _: True)
    service.close("bot", session.session_id)
    lease.release()


def test_persistent_close_all_cannot_flush_during_active_operation(tmp_path, monkeypatch):
    service, session, lease, _, factory = rig(tmp_path, monkeypatch)
    owned = service._sessions[session.session_id]
    owned.operation_lock.acquire()
    try:
        with pytest.raises(BrowserLimitError):
            service.close("bot", session.session_id)
        with pytest.raises(BrowserLaunchError):
            service.close_all()
        assert not any(method == "Browser.close" for method, _, _ in factory.transport.calls)
    finally:
        owned.operation_lock.release()
    service.close_all()
    lease.release()


def test_profile_owner_cannot_be_substituted(tmp_path):
    service, executable, launcher, _, _ = make_service(tmp_path)
    lease = BrowserProfileStore(tmp_path / "durable").acquire("account", "bot")
    try:
        with pytest.raises(BrowserOwnershipError):
            service.start("another-bot", executable_path=executable, profile_lease=lease)
        assert not launcher.specs
    finally:
        lease.release()
