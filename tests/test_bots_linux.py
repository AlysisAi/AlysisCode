"""Scoped Linux controller checks; real namespace coverage is scripts/qa/bots_linux.py."""

from pathlib import Path
from uuid import uuid4

import pytest

from alysis_code.bots.coding import CodingStore, WorkspaceRegistration
from alysis_code.bots.coding_worker import ScopedLinuxRunner
from alysis_code.bots.linux_worker import workspace_paths
from alysis_code.bots.models import BotInput
from alysis_code.bots.store import Conflict, NotFound, Store


def test_managed_registration_only_binds_its_own_bot_and_preserves_session(tmp_path):
    store = Store(tmp_path / "bots.db")
    coding = CodingStore(store, "owner")
    bot = store.save_bot("owner", BotInput(name="Bot", purpose="Scoped"))
    worker = uuid4().hex
    data = WorkspaceRegistration(id=bot["id"], worker_id=worker, name="Managed")
    first = coding.register(data, bot_id=bot["id"])
    second = coding.register(data, bot_id=bot["id"])
    assert first["session_id"] == second["session_id"]
    assert second["bot_id"] == bot["id"]
    with pytest.raises(NotFound):
        CodingStore(store, "other").register(data, bot_id=bot["id"])
    with pytest.raises(Conflict):
        coding.register(data.model_copy(update={"id": uuid4().hex}), bot_id=bot["id"])


def test_managed_provisioning_does_not_replace_explicit_workspace(tmp_path):
    store = Store(tmp_path / "bots.db")
    coding = CodingStore(store, "owner")
    bot = store.save_bot("owner", BotInput(name="Existing", purpose="Preserve"))
    worker = uuid4().hex
    existing = coding.register(
        WorkspaceRegistration(id=uuid4().hex, worker_id=worker, name="Explicit")
    )
    coding.bind(bot["id"], existing["id"])
    assert coding.managed_bots() == []
    with pytest.raises(Conflict, match="cannot be replaced"):
        coding.register(
            WorkspaceRegistration(id=bot["id"], worker_id=worker, name="Wrong"), bot_id=bot["id"]
        )
    assert coding.workspace_for(bot["id"])["id"] == existing["id"]


def test_workspace_provisioning_is_persistent_scoped_and_refuses_links(tmp_path):
    bot = uuid4().hex
    workspace, state = workspace_paths(tmp_path, bot)
    (workspace / "kept.txt").write_text("Kept")
    assert not state.is_relative_to(workspace)
    assert workspace_paths(tmp_path, bot) == (workspace, state)
    assert (workspace / "kept.txt").read_text() == "Kept"
    with pytest.raises(ValueError):
        workspace_paths(tmp_path, "../../outside")
    peer = uuid4().hex
    (tmp_path / "workspaces" / peer).symlink_to(workspace, target_is_directory=True)
    with pytest.raises(ValueError, match="links"):
        workspace_paths(tmp_path, peer)


def test_linux_commands_cannot_expand_the_registered_root(tmp_path, monkeypatch):
    from alysis_code.sandbox_runner import BwrapShellRunner

    captured = {}

    def run(self, **kw):
        captured.update(kw)
        return "receipt"

    monkeypatch.setattr(BwrapShellRunner, "run", run)
    runner = ScopedLinuxRunner(workspace=tmp_path, close_stdin=True)
    assert runner.run(root=tmp_path, cwd=tmp_path, cmd="git status", timeout_s=100) == "receipt"
    assert captured["timeout_s"] == 60
    assert captured["cmd"] == "git status"
    assert runner.network == "off" and runner.clear_env and runner.profile == "hardened"
    with pytest.raises(ValueError, match="leave the registered"):
        runner.run(root=Path("/"), cwd=tmp_path, cmd="true", timeout_s=1)
