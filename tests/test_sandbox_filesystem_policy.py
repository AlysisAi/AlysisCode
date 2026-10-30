"""The filesystem ceiling must survive runner selection and every mount path."""

from __future__ import annotations

import csv
import os

import pytest

import alysis_code.background_runner as background
import alysis_code.durable_service_manager as durable
import alysis_code.sandbox_runner as sandbox
from alysis_code.config import ConfigError
from alysis_code.sandbox_settings import ShellSandboxSettings


@pytest.fixture(autouse=True)
def no_platform_probes(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", os.defpath)
    monkeypatch.setattr(sandbox, "ensure_minimal_etc_dir", lambda **_: tmp_path)
    monkeypatch.setattr(sandbox, "_supports_bwrap_unshare_cgroup", lambda: False)
    monkeypatch.setattr(background, "_supports_bwrap_unshare_cgroup", lambda: False)
    monkeypatch.setattr(durable, "_supports_bwrap_unshare_cgroup", lambda: False)


def bwrap_args(root, *, filesystem_access="read-only", **overrides):
    kwargs = dict(
        root=root,
        cwd=root,
        cmd="git add .",
        network="off",
        clear_env=True,
        profile="hardened",
        unshare_cgroup=False,
        filesystem_access=filesystem_access,
    )
    kwargs.update(overrides)
    return sandbox._build_bwrap_argv(**kwargs)[0]


def docker_args(root, *, filesystem_access="read-only", **overrides):
    kwargs = dict(
        root=root,
        cwd=root,
        cmd="git add .",
        container_name="fixture",
        network="off",
        docker_image="fixture:local",
        clear_env=True,
        pids_limit=None,
        memory_limit=None,
        cpus=None,
        read_only_rootfs=False,
        protect_repo_meta=True,
        env_allowlist=(),
        filesystem_access=filesystem_access,
    )
    kwargs.update(overrides)
    return sandbox._build_docker_argv(**kwargs)[0]


@pytest.mark.parametrize("profile", ["compat", "hardened"])
@pytest.mark.parametrize("safe_git_exception", [False, True])
def test_bwrap_readonly_covers_safe_git_and_alternate_runtime_mounts(
    tmp_path,
    monkeypatch,
    profile,
    safe_git_exception,
):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".git").mkdir()
    # The engine Python runtime can also be mounted at its original host path.
    monkeypatch.setattr(sandbox, "_python_runtime_ro_bind_roots", lambda: (tmp_path,))
    args = bwrap_args(root, profile=profile, allow_repo_meta_writes=safe_git_exception)
    assert ["--ro-bind", os.fspath(root), "/workspace"] == args[
        args.index("/workspace") - 2 : args.index("/workspace") + 1
    ]
    assert "--bind" not in args
    assert "--bind-try" not in args
    assert os.fspath(tmp_path) in args


@pytest.mark.parametrize("safe_git_exception", [False, True])
@pytest.mark.parametrize("read_only_rootfs", [False, True])
def test_docker_readonly_is_recursive_and_has_no_overriding_metadata_volume(
    tmp_path,
    safe_git_exception,
    read_only_rootfs,
):
    (tmp_path / ".git").mkdir()
    args = docker_args(
        tmp_path,
        allow_repo_meta_writes=safe_git_exception,
        read_only_rootfs=read_only_rootfs,
    )
    mount = next(csv.reader([args[args.index("--mount") + 1]]))
    assert mount == [
        "type=bind",
        f"source={tmp_path.resolve()}",
        "target=/workspace",
        "readonly",
        "bind-recursive=readonly",
        "bind-propagation=rprivate",
    ]
    assert "-v" not in args
    assert "--privileged" not in args


def test_docker_mount_csv_cannot_inject_an_extra_mount_option(tmp_path):
    root = tmp_path / "comma,readonly=false"
    root.mkdir()
    args = docker_args(root)
    mount = next(csv.reader([args[args.index("--mount") + 1]]))
    assert len(mount) == 6
    assert mount[1] == f"source={root.resolve()}"
    assert "readonly=false" not in mount


@pytest.mark.parametrize("builder", [bwrap_args, docker_args])
def test_readonly_rejects_cwd_outside_workspace(tmp_path, builder):
    root = tmp_path / "workspace"
    root.mkdir()
    with pytest.raises(RuntimeError, match="outside"):
        builder(root, cwd=tmp_path)


@pytest.mark.parametrize("builder", [bwrap_args, docker_args])
def test_unknown_filesystem_policy_is_rejected(tmp_path, builder):
    with pytest.raises(ConfigError, match="filesystem policy"):
        builder(tmp_path, filesystem_access="typo")


def test_workspace_write_keeps_existing_mount_contract(tmp_path):
    args = bwrap_args(tmp_path, filesystem_access="workspace-write")
    assert args[args.index("--bind") + 1 : args.index("--bind") + 3] == [
        str(tmp_path),
        "/workspace",
    ]
    args = docker_args(tmp_path, filesystem_access="workspace-write")
    assert args[args.index("-v") + 1] == f"{tmp_path}:/workspace:rw"
    assert "--mount" not in args


@pytest.mark.parametrize(
    "backend,available", [("auto", True), ("seatbelt", True), ("seatbelt", False)]
)
def test_readonly_seatbelt_never_falls_back_to_host(backend, available):
    settings = ShellSandboxSettings(
        backend=backend, filesystem_access="read-only", unavailable_fallback="ask"
    )
    choice = sandbox.select_shell_sandbox_backend(
        settings, system="Darwin", which=lambda _: None, path_exists=lambda _: available
    )
    assert choice.backend == ("seatbelt" if available else None)
    assert not choice.requires_command_approval


@pytest.mark.parametrize("backend", ["bwrap", "docker"])
def test_factories_and_closed_stdin_keep_filesystem_ceiling(tmp_path, monkeypatch, backend):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name, **_: f"/usr/bin/{name}")
    settings = ShellSandboxSettings(backend=backend, filesystem_access="read-only")
    foreground = sandbox.build_shell_runner_from_settings(settings, tmp_path)
    verification = sandbox.with_closed_stdin(foreground)
    worker = background.build_background_shell_runner_from_settings(settings, tmp_path)
    assert (
        foreground.filesystem_access
        == verification.filesystem_access
        == worker.filesystem_access
        == "read-only"
    )
    assert verification.close_stdin is True


@pytest.mark.parametrize("execution", ["foreground", "background"])
@pytest.mark.parametrize("backend", ["bwrap", "docker"])
def test_runner_passes_filesystem_ceiling_to_mount_builder(
    tmp_path,
    monkeypatch,
    backend,
    execution,
):
    module = sandbox if execution == "foreground" else background
    runner_type = getattr(
        module, f"{backend.title()}{'Shell' if execution == 'foreground' else 'Background'}Runner"
    )

    class CapturedLaunch(Exception):
        pass

    def capture(**kwargs):
        assert kwargs["filesystem_access"] == "read-only"
        raise CapturedLaunch

    monkeypatch.setattr(module, f"_build_{backend}_argv", capture)
    runner = runner_type(filesystem_access="read-only")
    with pytest.raises(CapturedLaunch):
        if execution == "foreground":
            runner.run(root=tmp_path, cwd=tmp_path, cmd="git add .", timeout_s=1)
        else:
            runner.start(root=tmp_path, cwd=tmp_path, cmd="watch-command")


@pytest.mark.parametrize("backend", ["bwrap", "docker"])
def test_durable_preview_and_watch_launch_keep_filesystem_ceiling(tmp_path, monkeypatch, backend):
    monkeypatch.setattr(durable.platform, "system", lambda: "Linux")
    monkeypatch.setattr(durable.shutil, "which", lambda name, **_: f"/usr/bin/{name}")
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return [backend, "run", "fixture"], {}

    monkeypatch.setattr(durable, f"_build_{backend}_argv", capture)
    manager = durable.DurableServiceManager(
        root=tmp_path,
        state_dir=tmp_path / "services",
        settings=ShellSandboxSettings(backend=backend, filesystem_access="read-only"),
    )
    manager._build_launch(cmd="watch-command", cwd=tmp_path, service_id="fixture")
    assert captured["filesystem_access"] == "read-only"


@pytest.mark.parametrize("filesystem_access", ["read-only", "workspace-write"])
def test_docker_api_policy_preserves_existing_behavior_except_readonly(filesystem_access):
    inherited = {"DOCKER_API_VERSION": "1.43", "DOCKER_HOST": "tcp://fixture:2375"}
    actual = sandbox._docker_policy_env(inherited, filesystem_access)
    assert actual["DOCKER_API_VERSION"] == ("1.44" if filesystem_access == "read-only" else "1.43")
    assert actual["DOCKER_HOST"] == inherited["DOCKER_HOST"]
    assert inherited["DOCKER_API_VERSION"] == "1.43"
    defaults = sandbox._docker_policy_env({}, filesystem_access)
    assert defaults == ({"DOCKER_API_VERSION": "1.44"} if filesystem_access == "read-only" else {})


def test_docker_readonly_background_env_cannot_downgrade_api(tmp_path, monkeypatch):
    captured = {}

    class StopBeforeSpawn(Exception):
        pass

    def capture(argv, **kwargs):
        captured.update(kwargs)
        captured["argv"] = argv
        raise StopBeforeSpawn

    monkeypatch.setattr(background.subprocess, "Popen", capture)
    monkeypatch.setenv("DOCKER_API_VERSION", "1.43")
    runner = background.DockerBackgroundRunner(filesystem_access="read-only", clear_env=False)
    with pytest.raises(StopBeforeSpawn):
        runner.start(
            root=tmp_path,
            cwd=tmp_path,
            cmd="watch-command",
            env_overrides={
                "DOCKER_API_VERSION": "1.43",
                "docker_api_version": "1.42",
                "DOCKER_CONTEXT": "fixture-context",
            },
        )
    env = captured["env"]
    assert env["DOCKER_API_VERSION"] == "1.44"
    assert "docker_api_version" not in env
    assert env["DOCKER_CONTEXT"] == "fixture-context"
    assert "bind-recursive=readonly" in captured["argv"][captured["argv"].index("--mount") + 1]


def test_docker_readonly_durable_launch_binds_api_with_mounts(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_API_VERSION", "1.43")
    monkeypatch.setattr(durable.shutil, "which", lambda name, **_: f"/usr/bin/{name}")
    manager = durable.DurableServiceManager(
        root=tmp_path,
        state_dir=tmp_path / "services",
        settings=ShellSandboxSettings(
            backend="docker", filesystem_access="read-only", clear_env=False
        ),
    )
    launch = manager._build_launch(cmd="watch-command", cwd=tmp_path, service_id="fixture")
    assert launch["env"]["DOCKER_API_VERSION"] == "1.44"
    mount = launch["popen_args"][launch["popen_args"].index("--mount") + 1]
    assert "bind-recursive=readonly" in mount
    assert "bind-propagation=rprivate" in mount
