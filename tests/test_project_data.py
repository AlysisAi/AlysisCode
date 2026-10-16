from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from alysis_code.project_data import ProjectDataError, project_artifact_path, project_data_dir


def test_project_directory_preserves_run_pointer_bytes_and_inode(tmp_path):
    from alysis_code.forge import load_current_run_paths

    current = tmp_path / ".alysis"
    run = current / "runs/run-000001"
    run.mkdir(parents=True)
    payload = json.dumps({"run_id": "run-000001", "run_path": ".alysis/runs/run-000001"}).encode()
    (current / "current_run.json").write_bytes(payload)
    (run / "state.bin").write_bytes(b"history")
    inode = current.stat().st_ino
    assert project_data_dir(tmp_path) == current
    assert current.stat().st_ino == inode
    assert load_current_run_paths(tmp_path).run_dir == run
    assert (
        project_artifact_path(tmp_path, ".alysis/runs/run-000001/state.bin").read_bytes()
        == b"history"
    )
    assert (current / "current_run.json").read_bytes() == payload


def test_directory_resolution_does_not_create_state(tmp_path):
    assert project_data_dir(tmp_path) == tmp_path / ".alysis"
    assert not (tmp_path / ".alysis").exists()


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_project_data_rejects_files_and_symlinks(tmp_path, kind):
    path = tmp_path / ".alysis"
    if kind == "file":
        path.write_text("retained")
    else:
        target = tmp_path / "target"
        target.mkdir()
        try:
            path.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows account lacks symlink creation privilege")
            raise
    with pytest.raises(ProjectDataError, match="real directory"):
        project_data_dir(tmp_path)
    assert os.path.lexists(path)


def test_project_artifact_rejects_directory_escape(tmp_path):
    (tmp_path / ".alysis").mkdir()
    with pytest.raises(ProjectDataError, match="escapes"):
        project_artifact_path(tmp_path, ".alysis/../outside.txt")


def test_saved_asset_reads_canonical_locator_without_rewriting(tmp_path):
    from alysis_code.assets.asset_read_core import read_asset_raw_content

    saved = tmp_path / ".alysis/runs/run-000001/assets/raw/brief.txt"
    saved.parent.mkdir(parents=True)
    saved.write_text("Preserved attached project instructions")
    for locator in (str(saved), ".alysis/runs/run-000001/assets/raw/brief.txt"):
        record = _text_asset_record(locator)
        content = read_asset_raw_content(root=tmp_path, record=record, comprehension=None)
        assert "Preserved attached project instructions" in content
        assert record.stored_path == locator


def test_hook_command_and_hash_trust_are_preserved(tmp_path, monkeypatch):
    from alysis_code.hooks.config import load_resolved_hooks_config

    config_root = tmp_path / "config"
    workspace = tmp_path / "workspace"
    payload = _write_plugin_hook_project(workspace)
    trust_path = _trust_project_hooks(config_root, workspace, payload)
    trust_bytes = trust_path.read_bytes()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(config_root))
    assert project_data_dir(workspace) == workspace / ".alysis"
    [group] = load_resolved_hooks_config(workspace).groups_for_event("SessionStart")
    assert group.trusted
    script = workspace / ".alysis/hooks/plugins/example/notify.py"
    assert group.hooks[0].command == subprocess.list2cmdline([sys.executable, str(script)])
    assert (workspace / ".alysis/hooks.json").read_bytes() == payload
    assert trust_path.read_bytes() == trust_bytes
    (workspace / ".alysis/hooks.json").write_bytes(payload + b"\n")
    assert load_resolved_hooks_config(workspace).untrusted_project_paths


def _text_asset_record(locator: str):
    from alysis_code.assets.models import AssetRecord

    return AssetRecord.from_dict(
        {
            "id": "brief",
            "kind": "text",
            "stored_path": locator,
            "comprehension_status": "pending",
        }
    )


def _write_plugin_hook_project(workspace, *, description="Plugin hook from example"):
    import subprocess
    import sys

    script = workspace / ".alysis/hooks/plugins/example/notify.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('retained')")
    config_path = workspace / ".alysis/hooks.json"
    config_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "description": description,
                                    "command": subprocess.list2cmdline(
                                        [sys.executable, str(script)]
                                    ),
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )
    return config_path.read_bytes()


def _trust_project_hooks(config_root, workspace, payload):
    import hashlib

    from alysis_code.hooks.trust import trust_state_path

    trust_path = trust_state_path(user_config_dir=config_root)
    trust_path.parent.mkdir(parents=True, exist_ok=True)
    trust_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "trusted_configs": [
                    {
                        "workspace_root": str(workspace.resolve()),
                        "relative_config_path": ".alysis/hooks.json",
                        "file_hash": hashlib.sha256(payload).hexdigest(),
                    }
                ],
            }
        )
    )
    return trust_path


def test_foreign_workspace_asset_locator_is_not_rebased_into_this_project(tmp_path):
    from alysis_code.assets.asset_read_core import read_asset_raw_content

    workspace = tmp_path / "workspace"
    local = workspace / ".alysis/runs/run-000001/assets/raw/brief.txt"
    local.parent.mkdir(parents=True)
    local.write_text("This workspace only")
    foreign = tmp_path / "other/.alysis/runs/run-000001/assets/raw/brief.txt"
    record = _text_asset_record(str(foreign))
    content = read_asset_raw_content(root=workspace, record=record, comprehension=None)
    assert "This workspace only" not in content


def test_foreign_workspace_absolute_locator_is_not_rebased(tmp_path):
    foreign = tmp_path.parent / "another-workspace/.alysis/runs/old.txt"
    assert project_artifact_path(tmp_path, foreign) == foreign
    assert not (tmp_path / ".alysis").exists()
