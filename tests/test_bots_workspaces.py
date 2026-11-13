import os
import subprocess
import sys
from pathlib import Path

import pytest

from alysis_code.bots.workspaces import WorkspaceError, WorkspaceStore


def test_saved_bytes_survive_release_reopen_and_are_owner_scoped(tmp_path):
    root = tmp_path / "workspaces"
    store = WorkspaceStore(root)
    first = store.acquire("account", "bot")
    path = Path(first.name)
    content = bytes(range(256)) + "Αθήνα / 東京".encode()
    (path / "result.bin").write_bytes(content)
    assert not first.existed and not first.recovered
    with pytest.raises(WorkspaceError, match="already in use"):
        WorkspaceStore(root).acquire("account", "bot")
    first.cleanup()
    first.cleanup()
    reopened = WorkspaceStore(root).acquire("account", "bot")
    assert reopened.workspace_id == first.workspace_id
    assert reopened.existed and not reopened.recovered
    assert (Path(reopened.name) / "result.bin").read_bytes() == content
    for owner, bot in (("other", "bot"), ("account", "other")):
        isolated = store.acquire(owner, bot)
        assert not list(Path(isolated.name).iterdir())
        isolated.cleanup()
    reopened.cleanup()


def test_process_loss_releases_lock_and_reports_unclean_saved_workspace(tmp_path):
    root = tmp_path / "workspaces"
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import os,sys; "
            "from alysis_code.bots.workspaces import WorkspaceStore; "
            "lease=WorkspaceStore(Path(sys.argv[1])).acquire('account','bot'); "
            "Path(lease.name,'partial.txt').write_text('partial'); os._exit(0)",
            str(root),
        ],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        check=True,
    )
    assert process.returncode == 0
    lease = WorkspaceStore(root).acquire("account", "bot")
    assert lease.existed and lease.recovered
    assert Path(lease.name, "partial.txt").read_text() == "partial"
    lease.cleanup()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX links and modes; Windows test below")
def test_rejects_symlink_workspace_and_shared_storage(tmp_path):
    root = tmp_path / "workspaces"
    store = WorkspaceStore(root)
    lease = store.acquire("account", "bot")
    path = Path(lease.name)
    lease.cleanup()
    path.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="symbolic"):
        store.acquire("account", "bot")
    root.chmod(0o755)
    with pytest.raises(WorkspaceError, match="private"):
        WorkspaceStore(root)


def test_reopening_cleans_only_private_interrupted_transfer_staging(tmp_path):
    store = WorkspaceStore(tmp_path / "workspaces")
    lease = store.acquire("account", "bot")
    workspace = Path(lease.name)
    (workspace / "partial-user-file").write_text("retain")
    staging = workspace.parent / ".alysis-transfer-incomplete"
    staging.mkdir(mode=0o700)
    (staging / "payload").write_bytes(b"incomplete transfer")
    lease.cleanup(clean=False)
    recovered = store.acquire("account", "bot")
    assert recovered.recovered
    assert not staging.exists()
    assert (Path(recovered.name) / "partial-user-file").read_text() == "retain"
    recovered.cleanup()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions and lease locks")
def test_windows_refuses_a_second_lease_and_a_junction_workspace(tmp_path):
    import _winapi

    store = WorkspaceStore(tmp_path / "workspaces")
    lease = store.acquire("account", "bot")
    with pytest.raises(WorkspaceError, match="already in use"):
        store.acquire("account", "bot")
    path = Path(lease.name)
    lease.cleanup()
    path.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # Any Windows user can create a junction (symbolic links need a privilege).
    _winapi.CreateJunction(str(outside), str(path))
    with pytest.raises(WorkspaceError):
        store.acquire("account", "bot")
    assert list(outside.iterdir()) == []
