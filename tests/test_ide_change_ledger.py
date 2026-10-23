from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from alysis_code.ide import change_ledger as change_ledger_module
from alysis_code.ide.change_ledger import (
    ChangeLedger,
    ChangeLedgerError,
    StaleWorkspaceError,
)


def _ledger(workspace: Path, storage: Path, **kwargs: object) -> ChangeLedger:
    return ChangeLedger(workspace, storage_root=storage, **kwargs)


def test_checkpoint_presence_probe_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data = tmp_path / "data"
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(data))
    assert change_ledger_module.checkpoint_storage_exists(workspace) is False
    assert not data.exists()
    ChangeLedger(workspace)
    assert change_ledger_module.checkpoint_storage_exists(workspace) is True


def test_missing_git_has_a_safe_checkpoint_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def missing_git(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("private command path must not be displayed")

    monkeypatch.setattr(change_ledger_module.subprocess, "run", missing_git)
    with pytest.raises(ChangeLedgerError, match="Install Git") as error:
        _ledger(workspace, tmp_path / "checkpoints")
    assert "private command path" not in str(error.value)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_local_blob_hashes_match_git_without_per_file_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    object_format: str,
) -> None:
    monkeypatch.setenv("GIT_DEFAULT_HASH", object_format)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ledger = _ledger(workspace, tmp_path / "external")
    samples = [b"", b"hello\n", b"windows\r\nlines\r\n", bytes(range(256)), "γειά 🌍".encode()]
    for data in samples:
        expected = (
            ledger._git(["hash-object", "--stdin"], input_bytes=data, check=True)
            .stdout.decode()
            .strip()
        )
        assert ledger._blob_oid(data) == expected
        target = workspace / "sample.bin"
        target.write_bytes(data)
        original_git = ledger._git

        def no_git(*args: object, **kwargs: object) -> None:
            pytest.fail("File identity must not spawn Git")

        with monkeypatch.context() as scoped:
            scoped.setattr(ledger, "_git", no_git)
            assert ledger._path_identity(target) == ("100644", expected)
        assert ledger._git == original_git


def test_capture_revert_redo_and_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    storage = tmp_path / "external"
    workspace.mkdir()
    (workspace / "modify.txt").write_text("before\n", encoding="utf-8")
    (workspace / "delete.txt").write_text("remove me\n", encoding="utf-8")
    ledger = _ledger(workspace, storage)

    baseline = ledger.ensure_baseline("session-1")
    (workspace / "modify.txt").write_text("after\n", encoding="utf-8")
    (workspace / "create.txt").write_text("created\n", encoding="utf-8")
    (workspace / "delete.txt").unlink()
    turn = ledger.capture(
        "session-1",
        turn_id="turn-1",
        message="implement change",
        paths=("modify.txt", "create.txt", "delete.txt"),
    )

    assert turn.parent_id == baseline.checkpoint_id
    assert {(item.status, item.path) for item in turn.changes} == {
        ("A", "create.txt"),
        ("D", "delete.txt"),
        ("M", "modify.txt"),
    }
    rendered = ledger.diff("session-1", turn.checkpoint_id)
    assert "-before" in rendered["diff"]
    assert "+after" in rendered["diff"]
    assert rendered["truncated"] is False

    reverted = ledger.revert("session-1", turn.checkpoint_id)
    assert reverted.kind == "revert"
    assert (workspace / "modify.txt").read_text(encoding="utf-8") == "before\n"
    assert (workspace / "delete.txt").read_text(encoding="utf-8") == "remove me\n"
    assert not (workspace / "create.txt").exists()

    restarted = _ledger(workspace, storage)
    assert restarted.head("session-1") == reverted
    redone = restarted.redo("session-1")
    assert redone.kind == "redo"
    assert (workspace / "modify.txt").read_text(encoding="utf-8") == "after\n"
    assert (workspace / "create.txt").read_text(encoding="utf-8") == "created\n"
    assert not (workspace / "delete.txt").exists()


def test_revert_refuses_to_overwrite_newer_human_edit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    ledger = _ledger(workspace, tmp_path / "external")
    ledger.ensure_baseline("session-1")
    target.write_text("value = 2\n", encoding="utf-8")
    turn = ledger.capture("session-1", turn_id="turn-1", paths=("app.py",))

    target.write_text("value = 'human'\n", encoding="utf-8")
    with pytest.raises(StaleWorkspaceError, match="refusing to overwrite"):
        ledger.revert("session-1", turn.checkpoint_id)
    assert target.read_text(encoding="utf-8") == "value = 'human'\n"


def test_revert_preserves_edit_injected_at_each_path_commit_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    ledger = _ledger(workspace, tmp_path / "external")
    ledger.ensure_baseline("session-1")
    target.write_text("value = 2\n", encoding="utf-8")
    turn = ledger.capture("session-1", turn_id="turn-1", paths=("app.py",))
    original_restore = ledger._restore_one
    injected = False

    def restore_after_human_edit(*args: object, **kwargs: object) -> object:
        nonlocal injected
        if not injected:
            injected = True
            target.write_text("value = 'human'\n", encoding="utf-8")
        return original_restore(*args, **kwargs)

    monkeypatch.setattr(ledger, "_restore_one", restore_after_human_edit)

    with pytest.raises(StaleWorkspaceError, match="refusing to overwrite"):
        ledger.revert("session-1", turn.checkpoint_id)

    assert target.read_text(encoding="utf-8") == "value = 'human'\n"


def test_multi_path_revert_rolls_back_prior_paths_when_later_path_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "a.txt"
    second = workspace / "b.txt"
    first.write_text("a-before\n", encoding="utf-8")
    second.write_text("b-before\n", encoding="utf-8")
    ledger = _ledger(workspace, tmp_path / "external")
    ledger.ensure_baseline("session-1")
    first.write_text("a-agent\n", encoding="utf-8")
    second.write_text("b-agent\n", encoding="utf-8")
    turn = ledger.capture("session-1", turn_id="turn-1", paths=("a.txt", "b.txt"))
    original_restore = ledger._restore_one
    calls = 0

    def inject_on_second_restore(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            second.write_text("b-human\n", encoding="utf-8")
        return original_restore(*args, **kwargs)

    monkeypatch.setattr(ledger, "_restore_one", inject_on_second_restore)

    with pytest.raises(StaleWorkspaceError, match="refusing to overwrite"):
        ledger.revert("session-1", turn.checkpoint_id)

    assert first.read_text(encoding="utf-8") == "a-agent\n"
    assert second.read_text(encoding="utf-8") == "b-human\n"


def test_revert_detects_edit_after_per_path_identity_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("before\n", encoding="utf-8")
    ledger = _ledger(workspace, tmp_path / "external")
    ledger.ensure_baseline("session-1")
    target.write_text("agent\n", encoding="utf-8")
    turn = ledger.capture("session-1", turn_id="turn-1", paths=("app.py",))
    original_identity = ledger._path_identity
    target_identity_checks = 0

    def identity_then_inject(path: Path) -> tuple[str, str] | None:
        nonlocal target_identity_checks
        result = original_identity(path)
        if path == target:
            target_identity_checks += 1
            # First check is the operation-wide guard. The second is the final
            # per-path check immediately before the namespace transaction.
            if target_identity_checks == 2:
                target.write_text("human\n", encoding="utf-8")
        return result

    monkeypatch.setattr(ledger, "_path_identity", identity_then_inject)

    with pytest.raises(StaleWorkspaceError, match="refusing to overwrite"):
        ledger.revert("session-1", turn.checkpoint_id)

    assert target.read_text(encoding="utf-8") == "human\n"


def test_revert_deletion_detects_path_recreated_at_commit_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "created-by-agent.txt"
    ledger = _ledger(workspace, tmp_path / "external")
    ledger.ensure_baseline("session-1")
    target.write_text("agent\n", encoding="utf-8")
    turn = ledger.capture("session-1", turn_id="turn-1", paths=("created-by-agent.txt",))
    original_install = ledger._install_tree_entry_no_replace
    injected = False

    def install_after_human_create(*args: object, **kwargs: object) -> None:
        nonlocal injected
        if not injected:
            injected = True
            target.write_text("human\n", encoding="utf-8")
        original_install(*args, **kwargs)

    monkeypatch.setattr(ledger, "_install_tree_entry_no_replace", install_after_human_create)

    with pytest.raises(StaleWorkspaceError, match="refusing to overwrite"):
        ledger.revert("session-1", turn.checkpoint_id)

    assert target.read_text(encoding="utf-8") == "human\n"


def test_revert_restores_displaced_file_after_non_collision_publish_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("before\n", encoding="utf-8")
    ledger = _ledger(workspace, tmp_path / "external")
    ledger.ensure_baseline("session-1")
    target.write_text("agent\n", encoding="utf-8")
    turn = ledger.capture("session-1", turn_id="turn-1", paths=("app.py",))
    original_publish = change_ledger_module._publish_entry_no_replace

    def fail_checkpoint_publication(source: Path, destination: Path) -> bool:
        if source.suffix == ".staged":
            raise PermissionError("injected publication failure")
        return original_publish(source, destination)

    monkeypatch.setattr(
        change_ledger_module,
        "_publish_entry_no_replace",
        fail_checkpoint_publication,
    )

    with pytest.raises(ChangeLedgerError, match="Unable to restore the checkpoint safely"):
        ledger.revert("session-1", turn.checkpoint_id)

    assert target.read_text(encoding="utf-8") == "agent\n"
    assert not tuple(workspace.glob(".app.py.*.displaced"))


def test_checkpoint_restore_preserves_symlink_entries(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "before.txt").write_text("before\n", encoding="utf-8")
    (workspace / "after.txt").write_text("after\n", encoding="utf-8")
    link = workspace / "current.txt"
    try:
        os.symlink("before.txt", link)
    except OSError:
        pytest.skip("The current platform account cannot create symbolic links")
    ledger = _ledger(workspace, tmp_path / "external")
    ledger.ensure_baseline("session-1")
    link.unlink()
    os.symlink("after.txt", link)
    turn = ledger.capture("session-1", turn_id="turn-1", paths=("current.txt",))

    reverted = ledger.revert("session-1", turn.checkpoint_id)

    assert reverted.kind == "revert"
    assert link.is_symlink()
    assert os.readlink(link) == "before.txt"
    ledger.redo("session-1")
    assert link.is_symlink()
    assert os.readlink(link) == "after.txt"


def test_sensitive_files_are_never_stored_but_templates_are(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".aws").mkdir()
    (workspace / ".kube").mkdir()
    (workspace / "keys").mkdir()
    (workspace / ".env").write_text("TOKEN=top-secret\n", encoding="utf-8")
    (workspace / ".env.local").write_text("TOKEN=also-secret\n", encoding="utf-8")
    (workspace / ".env.example").write_text("TOKEN=replace-me\n", encoding="utf-8")
    (workspace / ".npmrc").write_text("token=npm-checkpoint-secret\n", encoding="utf-8")
    (workspace / ".aws" / "credentials").write_text(
        "secret=cloud-checkpoint-secret\n", encoding="utf-8"
    )
    (workspace / ".kube" / "config").write_text("token=kube-checkpoint-secret\n", encoding="utf-8")
    (workspace / "keys" / "deploy.ppk").write_text("ppk-checkpoint-secret\n", encoding="utf-8")
    (workspace / "secrets.yaml").write_text("token: yaml-checkpoint-secret\n", encoding="utf-8")
    (workspace / "debug.keystore").write_text("keystore-checkpoint-secret\n", encoding="utf-8")
    ledger = _ledger(workspace, tmp_path / "external")

    baseline = ledger.ensure_baseline("session-1")

    assert ".env.example" in {item.path for item in baseline.changes}
    assert ".env" not in {item.path for item in baseline.changes}
    assert ".env.local" not in {item.path for item in baseline.changes}
    assert ".npmrc" not in {item.path for item in baseline.changes}
    assert ".aws/credentials" not in {item.path for item in baseline.changes}
    assert ".kube/config" not in {item.path for item in baseline.changes}
    assert "keys/deploy.ppk" not in {item.path for item in baseline.changes}
    assert "secrets.yaml" not in {item.path for item in baseline.changes}
    assert "debug.keystore" not in {item.path for item in baseline.changes}
    storage_bytes = b"".join(
        path.read_bytes()
        for path in ledger.storage_dir.rglob("*")
        if path.is_file() and path.stat().st_size < 5_000_000
    )
    assert b"top-secret" not in storage_bytes
    assert b"also-secret" not in storage_bytes
    assert b"npm-checkpoint-secret" not in storage_bytes
    assert b"cloud-checkpoint-secret" not in storage_bytes
    assert b"kube-checkpoint-secret" not in storage_bytes
    assert b"ppk-checkpoint-secret" not in storage_bytes
    assert b"yaml-checkpoint-secret" not in storage_bytes
    assert b"keystore-checkpoint-secret" not in storage_bytes


def test_storage_must_be_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ChangeLedgerError, match="outside"):
        _ledger(workspace, workspace / ".checkpoints")


def test_large_file_is_omitted_without_losing_small_changes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "small.txt").write_text("ok", encoding="utf-8")
    (workspace / "large.bin").write_bytes(os.urandom(64))
    ledger = _ledger(
        workspace,
        tmp_path / "external",
        max_file_bytes=16,
        max_snapshot_bytes=32,
    )

    baseline = ledger.ensure_baseline("session-1")

    assert "large.bin" in baseline.omitted_paths
    assert "large.bin" in baseline.non_revertible_paths
    assert "small.txt" in {item.path for item in baseline.changes}


def test_baseline_omission_is_never_staged_or_destroyed_by_revert(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    large = workspace / "large.bin"
    safe = workspace / "safe.txt"
    large.write_bytes(b"baseline-bytes-that-cannot-be-captured")
    safe.write_text("before\n", encoding="utf-8")
    ledger = _ledger(
        workspace,
        tmp_path / "external",
        max_file_bytes=16,
        max_snapshot_bytes=32,
    )

    baseline = ledger.ensure_baseline("session-1")
    assert "large.bin" in baseline.omitted_paths
    assert "large.bin" in baseline.non_revertible_paths

    large.write_bytes(b"new")
    safe.write_text("after\n", encoding="utf-8")
    turn = ledger.capture(
        "session-1",
        turn_id="turn-1",
        paths=("large.bin", "safe.txt"),
    )

    assert "large.bin" in turn.omitted_paths
    assert "large.bin" in turn.non_revertible_paths
    assert {record.path for record in turn.changes} == {"safe.txt"}

    ledger.revert("session-1", turn.checkpoint_id)
    assert large.read_bytes() == b"new"
    assert safe.read_text(encoding="utf-8") == "before\n"
    ledger.redo("session-1")
    assert large.read_bytes() == b"new"
    assert safe.read_text(encoding="utf-8") == "after\n"


def test_sensitive_baseline_omission_stays_explicit_and_preserved(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = workspace / ".env"
    safe = workspace / "safe.txt"
    secret.write_text("TOKEN=before\n", encoding="utf-8")
    safe.write_text("before\n", encoding="utf-8")
    ledger = _ledger(workspace, tmp_path / "external")

    baseline = ledger.ensure_baseline("session-1")
    assert ".env" in baseline.omitted_paths
    assert ".env" in baseline.non_revertible_paths

    secret.write_text("TOKEN=after\n", encoding="utf-8")
    safe.write_text("after\n", encoding="utf-8")
    turn = ledger.capture("session-1", turn_id="turn-1", paths=(".env", "safe.txt"))
    assert ".env" in turn.omitted_paths
    assert ".env" in turn.non_revertible_paths
    assert {record.path for record in turn.changes} == {"safe.txt"}

    ledger.revert("session-1", turn.checkpoint_id)
    assert secret.read_text(encoding="utf-8") == "TOKEN=after\n"
    assert safe.read_text(encoding="utf-8") == "before\n"


def test_session_lineage_adoption_is_owned_idempotent_and_reversible(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("before\n", encoding="utf-8")
    ledger = _ledger(workspace, tmp_path / "external")
    ledger.ensure_baseline("old-session")
    target.write_text("after\n", encoding="utf-8")
    turn = ledger.capture("old-session", turn_id="turn-1", paths=("app.py",))
    ledger.ensure_baseline("unrelated-session")

    assert ledger.adopt_session("new-session", "old-session") == "old-session"
    assert ledger.adopt_session("new-session", "old-session") == "old-session"
    assert ledger.head("new-session") == turn
    assert ledger.diff("new-session", turn.checkpoint_id)["checkpoint_id"] == turn.checkpoint_id
    with pytest.raises(ChangeLedgerError, match="not found for this session"):
        ledger.diff("unrelated-session", turn.checkpoint_id)

    ledger.revert("new-session", turn.checkpoint_id)
    assert target.read_text(encoding="utf-8") == "before\n"
    ledger.redo("new-session")
    assert target.read_text(encoding="utf-8") == "after\n"

    ledger.ensure_baseline("other-lineage")
    with pytest.raises(ChangeLedgerError, match="different checkpoint lineage"):
        ledger.adopt_session("new-session", "other-lineage")


def test_legacy_lineage_migration_disables_unsafe_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("before\n", encoding="utf-8")
    storage = tmp_path / "external"
    ledger = _ledger(workspace, storage)
    ledger.ensure_baseline("legacy-session")
    target.write_text("after\n", encoding="utf-8")
    turn = ledger.capture("legacy-session", turn_id="turn-1", paths=("app.py",))

    with sqlite3.connect(ledger.database_path) as db:
        db.execute("DELETE FROM ledger_schema WHERE key = 'omission_safety_version'")

    original_commit_metadata = ChangeLedger._commit_metadata

    def legacy_commit_metadata(
        self: ChangeLedger, commit_oid: str
    ) -> tuple[dict[str, object], str, str]:
        metadata, message, created_at = original_commit_metadata(self, commit_oid)
        metadata = dict(metadata)
        metadata["version"] = 1
        metadata.pop("non_revertible_paths", None)
        return metadata, message, created_at

    monkeypatch.setattr(ChangeLedger, "_commit_metadata", legacy_commit_metadata)
    restarted = _ledger(workspace, storage)
    recovered = restarted.head("legacy-session")
    assert recovered is not None
    assert "*" in recovered.non_revertible_paths

    with pytest.raises(ChangeLedgerError, match="non-revertible restore"):
        restarted.revert("legacy-session", turn.checkpoint_id)
    assert target.read_text(encoding="utf-8") == "after\n"


def test_scoped_checkpoint_never_absorbs_or_reverts_unrelated_human_edit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent_file = workspace / "agent.txt"
    human_file = workspace / "human.txt"
    agent_file.write_text("before-agent\n", encoding="utf-8")
    human_file.write_text("before-human\n", encoding="utf-8")
    ledger = _ledger(workspace, tmp_path / "external")
    ledger.ensure_baseline("session-1")

    agent_file.write_text("after-agent\n", encoding="utf-8")
    human_file.write_text("after-human\n", encoding="utf-8")
    turn = ledger.capture("session-1", turn_id="turn-1", paths=("agent.txt",))

    assert {record.path for record in turn.changes} == {"agent.txt"}
    ledger.revert("session-1", turn.checkpoint_id)
    assert agent_file.read_text(encoding="utf-8") == "before-agent\n"
    assert human_file.read_text(encoding="utf-8") == "after-human\n"
    ledger.redo("session-1")
    assert agent_file.read_text(encoding="utf-8") == "after-agent\n"
    assert human_file.read_text(encoding="utf-8") == "after-human\n"


def test_capture_fails_closed_when_scoped_path_changes_while_git_stages_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("before\n", encoding="utf-8")
    ledger = _ledger(workspace, tmp_path / "external")
    baseline = ledger.ensure_baseline("session-1")
    target.write_text("agent\n", encoding="utf-8")
    original_git = ledger._git
    injected = False

    def git_then_human_edit(args: list[str], **kwargs: object) -> object:
        nonlocal injected
        result = original_git(args, **kwargs)
        if args[:1] == ["add"] and not injected:
            injected = True
            target.write_text("human\n", encoding="utf-8")
        return result

    monkeypatch.setattr(ledger, "_git", git_then_human_edit)

    with pytest.raises(StaleWorkspaceError, match="being captured"):
        ledger.capture("session-1", turn_id="turn-1", paths=("app.py",))

    assert ledger.head("session-1") == baseline
    assert target.read_text(encoding="utf-8") == "human\n"


def test_reconcile_reads_every_session_ref_with_one_git_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    storage = tmp_path / "external"
    workspace.mkdir()
    (workspace / "app.py").write_text("one\n", encoding="utf-8")
    ledger = _ledger(workspace, storage)
    sessions = [f"session-{index}" for index in range(6)]
    for session in sessions:
        ledger.ensure_baseline(session)
    calls: list[str] = []

    def count_git(target: ChangeLedger) -> None:
        original = target._git

        def counting_git(args: list[str], **kwargs: Any) -> Any:
            calls.append(args[0])
            return original(args, **kwargs)

        monkeypatch.setattr(target, "_git", counting_git)

    count_git(ledger)
    for session in sessions:
        assert ledger.head(session) is not None
    # Each look reconciles all six sessions: one process each time, not one per session.
    assert calls == ["for-each-ref"] * len(sessions)
    calls.clear()
    restarted = _ledger(workspace, storage)
    count_git(restarted)
    assert [checkpoint.kind for checkpoint in restarted.list("session-2")] == ["baseline"]
    assert calls == ["for-each-ref"]


def test_reconcile_restores_a_session_ref_deleted_after_its_checkpoint(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    storage = tmp_path / "external"
    workspace.mkdir()
    (workspace / "app.py").write_text("one\n", encoding="utf-8")
    ledger = _ledger(workspace, storage)
    kept = ledger.ensure_baseline("session-kept")
    lost = ledger.ensure_baseline("session-lost")
    ref = "refs/heads/sessions/" + change_ledger_module._session_key("session-lost")
    ledger._git(["update-ref", "-d", ref], check=True)
    assert ledger._ref_oid(ref) is None

    restarted = _ledger(workspace, storage)
    assert restarted._ref_oid(ref) == lost.commit_oid
    assert restarted.head("session-lost") == lost
    assert restarted.head("session-kept") == kept


def test_restart_recovers_ref_published_before_sqlite_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    storage = tmp_path / "external"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("before\n", encoding="utf-8")
    ledger = _ledger(workspace, storage)
    baseline = ledger.ensure_baseline("session-1")
    target.write_text("after\n", encoding="utf-8")

    def crash_after_ref(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise RuntimeError("simulated process death after update-ref")

    monkeypatch.setattr(ledger, "_changes_between", crash_after_ref)
    with pytest.raises(RuntimeError, match="simulated process death"):
        ledger.capture("session-1", turn_id="turn-1", paths=("app.py",))

    restarted = _ledger(workspace, storage)
    recovered = restarted.head("session-1")
    assert recovered is not None
    assert recovered.parent_id == baseline.checkpoint_id
    assert recovered.turn_id == "turn-1"
    assert recovered.kind == "turn"
    assert {record.path for record in recovered.changes} == {"app.py"}
    assert target.read_text(encoding="utf-8") == "after\n"


def test_restart_rolls_back_partial_restore_without_touching_external_edits(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    storage = tmp_path / "external"
    workspace.mkdir()
    first = workspace / "a.txt"
    second = workspace / "b.txt"
    first.write_text("a-before\n", encoding="utf-8")
    second.write_text("b-before\n", encoding="utf-8")
    ledger = _ledger(workspace, storage)
    baseline = ledger.ensure_baseline("session-1")
    first.write_text("a-agent\n", encoding="utf-8")
    second.write_text("b-agent\n", encoding="utf-8")
    turn = ledger.capture("session-1", turn_id="turn-1", paths=("a.txt", "b.txt"))
    pending = ledger._pending_restore_payload(
        session_id="session-1",
        action="revert",
        source=turn,
        restore_oid=baseline.commit_oid,
        paths=("a.txt", "b.txt"),
        turn_id=turn.turn_id,
        step_id=turn.step_id,
        kind="revert",
        message=f"Revert {turn.checkpoint_id}",
        reverts_id=turn.checkpoint_id,
    )
    ledger._write_pending_operation(pending)
    mutation = ledger._restore_one(
        baseline.commit_oid,
        "a.txt",
        expected_identity=ledger._tree_identity(turn.commit_oid, "a.txt"),
    )
    ledger._finalize_restore(mutation)
    second.write_text("b-human\n", encoding="utf-8")

    restarted = _ledger(workspace, storage)

    assert restarted.head("session-1") == turn
    assert first.read_text(encoding="utf-8") == "a-agent\n"
    assert second.read_text(encoding="utf-8") == "b-human\n"
    assert not restarted.pending_operation_path.exists()


def test_restart_finishes_restore_completed_before_checkpoint_metadata(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    storage = tmp_path / "external"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("before\n", encoding="utf-8")
    ledger = _ledger(workspace, storage)
    baseline = ledger.ensure_baseline("session-1")
    target.write_text("agent\n", encoding="utf-8")
    turn = ledger.capture("session-1", turn_id="turn-1", paths=("app.py",))
    pending = ledger._pending_restore_payload(
        session_id="session-1",
        action="revert",
        source=turn,
        restore_oid=baseline.commit_oid,
        paths=("app.py",),
        turn_id=turn.turn_id,
        step_id=turn.step_id,
        kind="revert",
        message=f"Revert {turn.checkpoint_id}",
        reverts_id=turn.checkpoint_id,
    )
    ledger._write_pending_operation(pending)
    ledger._restore_paths(baseline.commit_oid, ("app.py",), rollback_oid=turn.commit_oid)

    restarted = _ledger(workspace, storage)
    recovered = restarted.head("session-1")

    assert recovered is not None
    assert recovered.checkpoint_id == pending["checkpoint_id"]
    assert recovered.kind == "revert"
    assert recovered.reverts_id == turn.checkpoint_id
    assert target.read_text(encoding="utf-8") == "before\n"
    assert not restarted.pending_operation_path.exists()


def test_restart_restores_source_displaced_before_replacement_publish(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    storage = tmp_path / "external"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("before\n", encoding="utf-8")
    ledger = _ledger(workspace, storage)
    baseline = ledger.ensure_baseline("session-1")
    target.write_text("agent\n", encoding="utf-8")
    turn = ledger.capture("session-1", turn_id="turn-1", paths=("app.py",))
    pending = ledger._pending_restore_payload(
        session_id="session-1",
        action="revert",
        source=turn,
        restore_oid=baseline.commit_oid,
        paths=("app.py",),
        turn_id=turn.turn_id,
        step_id=turn.step_id,
        kind="revert",
        message=f"Revert {turn.checkpoint_id}",
        reverts_id=turn.checkpoint_id,
    )
    ledger._write_pending_operation(pending)
    displaced = ledger._displace_matching(
        target,
        rel_path="app.py",
        expected_identity=ledger._tree_identity(turn.commit_oid, "app.py"),
    )

    assert displaced is not None
    assert not target.exists()

    restarted = _ledger(workspace, storage)

    assert restarted.head("session-1") == turn
    assert target.read_text(encoding="utf-8") == "agent\n"
    assert not tuple(workspace.glob(".app.py.*.displaced"))
    assert not restarted.pending_operation_path.exists()
