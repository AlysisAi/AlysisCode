from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from alysis_code.project_folders import (
    allowed_roots,
    folder_scope,
    turn_tools,
    validate_folders,
    validate_worker_folders,
)
from alysis_code.sandbox_runner import _build_bwrap_argv, seatbelt_command
from alysis_code.tools.fs import FsError, fs_read, fs_write
from alysis_code.tools.search import search_rg


def test_explicit_roots_do_not_grant_parent_or_siblings_and_are_revoked(tmp_path):
    main, extra, other = [tmp_path / name for name in ["main", "日本語", "other"]]
    for folder in (main, extra, other):
        folder.mkdir()
        (folder / "data.txt").write_text(folder.name, encoding="utf-8")
    with folder_scope(main, validate_folders([str(extra)])):
        assert "日本語" in fs_read(root=main, path=str(extra / "data.txt"))["content"]
        fs_write(root=main, path=str(extra / "new.txt"), content="hello")
        assert search_rg(root=main, root_path=str(extra), pattern="hello")["matches"]
        for path in (other / "data.txt", tmp_path / "data.txt"):
            with pytest.raises(FsError):
                fs_read(root=main, path=str(path))
    assert allowed_roots(main) == (main,)
    with pytest.raises(FsError):
        fs_read(root=main, path=str(extra / "data.txt"))
    assert (extra / "new.txt").read_text() == "hello"


def test_project_folder_symlink_cannot_expand_access(tmp_path):
    main, extra, other = [tmp_path / name for name in ["main", "extra", "other"]]
    for folder in (main, extra, other):
        folder.mkdir()
    (other / "data.txt").write_text("outside")
    try:
        (extra / "escape").symlink_to(other, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account lacks symlink creation privilege")
        raise
    with folder_scope(main, [str(extra)]):
        with pytest.raises(FsError):
            fs_read(root=main, path=str(extra / "escape/data.txt"))


def test_real_tool_wrappers_read_edit_and_protected_paths(tmp_path, monkeypatch):
    from test_read_ledger_delivery_boundary import _Client, _session

    # Exercise tool admission independently of an installed OS sandbox. The
    # contained-worker transport and backend policies are tested below.
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    agent = _session(tmp_path, monkeypatch, _Client([]))
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "data.txt").write_text("before")
    (extra / ".git").mkdir()
    agent.project_folders = [str(extra)]
    try:
        tools, prompt = turn_tools(agent, agent.tools)
        assert str(extra) in json.loads(prompt.splitlines()[1])["folders"]
        # Real dispatch in a different thread must retain this call's scope.
        with ThreadPoolExecutor() as executor:
            result = executor.submit(
                tools["fs_read"].run, {"path": str(extra / "data.txt")}
            ).result()
        assert "before" in result["content"], result
        result = tools["fs_write"].run({"path": str(extra / "data.txt"), "content": "after"})
        assert (extra / "data.txt").read_text() == "after", result
        with pytest.raises(
            Exception, match="protected|User declined" if os.name == "nt" else "protected"
        ):
            tools["fs_write"].run({"path": str(extra / ".git/config"), "content": "blocked"})
        assert not (extra / ".git/config").exists()
        agent.project_folders = []
        revoked, _ = turn_tools(agent, agent.tools)
        with pytest.raises(Exception, match="escapes|outside|External file access is unavailable"):
            revoked["fs_read"].run({"path": str(extra / "data.txt")})
    finally:
        agent.close()


def test_sandbox_commands_mount_only_authorized_roots(tmp_path, monkeypatch):
    # Mount construction is independent of this developer machine's SDK PATH.
    monkeypatch.setenv("PATH", os.defpath)
    main, extra = tmp_path / "main", tmp_path / "extra"
    main.mkdir()
    extra.mkdir()
    (extra / ".git").mkdir()
    with folder_scope(main, [str(extra)]):
        command = seatbelt_command(
            "pwd", root=main, network="off", protect_repo_meta=True, safe_git_writes=True
        )
        assert f"FOLDER_0={extra}" in command
        assert str(extra / ".git") in command
        args, _ = _build_bwrap_argv(
            root=main,
            cwd=extra,
            cmd="pwd",
            network="off",
            clear_env=True,
            profile="hardened",
            unshare_cgroup=False,
        )
        assert ["--bind", str(extra), str(extra)] == args[
            args.index(str(extra)) - 1 : args.index(str(extra)) + 2
        ]
        assert str(tmp_path) not in args
        assert str(extra) in args


@pytest.mark.parametrize("validate", [validate_folders, validate_worker_folders])
def test_validate_directories(tmp_path, validate):
    assert validate([str(tmp_path), str(tmp_path)]) == [str(tmp_path)]
    for value in ("/tmp", ["relative"], ["/"], [str(tmp_path / "missing")]):
        with pytest.raises((ValueError, OSError)):
            validate(value)


def test_host_still_rejects_home_and_worker_rejects_noncanonical_descriptors(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(ValueError, match="home root"):
        validate_folders([str(tmp_path)])
    extra = tmp_path / "extra"
    extra.mkdir()
    with pytest.raises(ValueError, match="canonical"):
        validate_worker_folders([str(extra / ".." / "extra")])


@pytest.mark.parametrize("access", ["workspace-write", "read-only"])
def test_project_roots_use_native_policy_and_revoke_between_calls(tmp_path, monkeypatch, access):
    from alysis_code import windows_sandbox as native
    from alysis_code.config import ConfigError
    from alysis_code.sandbox_settings import ShellSandboxSettings
    from alysis_code.sandbox_workspace import contains_workspace_path

    main, extra, helper = [tmp_path / name for name in ("main", "extra", "helper")]
    for path in (main, extra, helper):
        path.mkdir()
    monkeypatch.setattr(native, "_powershell_execution_policy", lambda _: "RemoteSigned")
    settings = ShellSandboxSettings(filesystem_access=access)
    kwargs = dict(root=main, cwd=extra, cmd="pwd", helper=helper / "helper.exe")
    with folder_scope(main, [str(extra)]):
        assert contains_workspace_path(main, extra)
        request = native.build_request(settings, **kwargs)
        assert str(extra) in request["read"]
        assert (str(extra) in request["write"]) == (access == "workspace-write")
        assert extra / ".git" in map(Path, request["deny_write"])
        assert str(tmp_path) not in request["read"]
    assert not contains_workspace_path(main, extra)
    with pytest.raises(ConfigError, match="approved workspace"):
        native.build_request(settings, **kwargs)
    assert settings.additional_workspace_roots == ()


def test_readonly_backends_keep_all_project_mounts_readonly(tmp_path, monkeypatch):
    from alysis_code.sandbox_runner import _build_docker_argv

    monkeypatch.setenv("PATH", os.defpath)
    main = tmp_path / "main"
    extra = tmp_path / ("extra, 'quoted'" if os.name == "nt" else 'extra, "quoted"')
    main.mkdir()
    extra.mkdir()
    with folder_scope(main, [str(extra)]):
        args, _ = _build_bwrap_argv(
            root=main,
            cwd=extra,
            cmd="pwd",
            network="off",
            clear_env=True,
            profile="hardened",
            unshare_cgroup=False,
            filesystem_access="read-only",
        )
        for index, value in enumerate(args):
            if value == str(extra) and index and args[index - 1].startswith("--"):
                assert args[index - 1] in {"--ro-bind", "--chdir"}
        docker_options = dict(
            root=main,
            cwd=extra,
            cmd="pwd",
            container_name="test",
            network="off",
            docker_image="test",
            clear_env=True,
            pids_limit=None,
            memory_limit=None,
            cpus=None,
            read_only_rootfs=True,
            protect_repo_meta=True,
            env_allowlist=(),
            filesystem_access="read-only",
        )
        if os.name == "nt":
            from alysis_code.config import ConfigError

            with pytest.raises(ConfigError, match="Additional Docker folders require POSIX paths"):
                _build_docker_argv(**docker_options)
            return
        docker, _ = _build_docker_argv(**docker_options)
        mounts = [docker[i + 1] for i, value in enumerate(docker) if value == "--mount"]
        assert len(mounts) == 2
        assert all("bind-recursive=readonly" in value for value in mounts)
        assert "-v" not in docker


@pytest.mark.parametrize("home_available", [True, False])
def test_contained_worker_receives_scoped_roots_and_cannot_expand_them(
    tmp_path, monkeypatch, capsys, home_available
):
    import base64
    import json
    import sys
    from dataclasses import asdict

    from alysis_code.contained_read_tools import ContainedReadTools
    from alysis_code.host_read_worker import main as worker_main
    from alysis_code.project_folders import relative_path
    from alysis_code.sandbox_settings import ShellSandboxSettings
    from alysis_code.tools.fs import capture_file_precondition

    main, extra, other = [tmp_path / name for name in ("main", "extra", "other")]
    for path in (main, extra, other):
        path.mkdir()
        (path / "data.txt").write_text("before")
    worker = ContainedReadTools(ShellSandboxSettings(), main)
    monkeypatch.chdir(main)

    def dispatch(raw):
        monkeypatch.setattr(sys, "argv", ["worker", base64.b64encode(raw).decode()])
        code = worker_main()
        return code, json.loads(capsys.readouterr().out)

    with folder_scope(main, [str(extra)]):
        assert relative_path(main, extra / "data.txt") == str(extra / "data.txt")
        read = worker._request("fs_list", {"root_path": str(extra)})
        write = worker._request(
            "fs_write_prepared",
            {
                "path": str(extra / "data.txt"),
                "content": "after",
                "precondition": asdict(
                    capture_file_precondition(root=main, path=str(extra / "data.txt"))
                ),
            },
        )
    # The worker is a separate process in production: no inherited folder scope.
    assert allowed_roots(main) == (main,)
    if not home_available:

        def unavailable_home():
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(Path, "home", unavailable_home)
    read_result = dispatch(read)
    assert read_result[0] == 0, read_result
    assert dispatch(write)[0] == 0
    assert (extra / "data.txt").read_text() == "after"
    assert dispatch(worker._request("fs_list", {"root_path": str(extra)}))[0] == 1
    request = json.loads(read)
    request["arguments"]["root_path"] = str(other)
    assert dispatch(json.dumps(request).encode())[0] == 1
    request["arguments"]["root"] = str(other)
    assert dispatch(json.dumps(request).encode())[0] == 1
    assert allowed_roots(main) == (main,)
