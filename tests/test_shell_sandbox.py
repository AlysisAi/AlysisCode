from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

import alysis_code.agent_loop as agent_loop_mod
import alysis_code.sandbox_runner as sandbox_runner_mod
import alysis_code.tools.shell as shell_mod
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig, ConfigError
from alysis_code.sandbox_runner import (
    BwrapShellRunner,
    DisabledShellRunner,
    DockerShellRunner,
    HostShellRunner,
    build_shell_runner,
)
from alysis_code.sandbox_settings import resolve_shell_sandbox_settings

TEST_DOCKER_IMAGE = "test/alysis-sandbox:dev"


@pytest.fixture(autouse=True)
def controlled_host_path(monkeypatch):
    # Linux argv tests run on every host; do not inspect unrelated host SDKs.
    # Discovery tests install their own executable fixtures on PATH explicitly.
    monkeypatch.setenv("PATH", os.defpath)


def _cfg(
    *,
    mode: str = "off",
    backend: str = "auto",
    network: str = "off",
    preview_access: str = "auto",
    docker_image: str = TEST_DOCKER_IMAGE,
    clear_env: bool = False,
    docker_pids_limit: int | None = None,
    docker_memory: str | None = None,
    docker_cpus: str | None = None,
    docker_read_only: bool = False,
    protect_repo_meta: bool = False,
    docker_env_allowlist: list[str] | None = None,
) -> AppConfig:
    cfg = AppConfig(model="test-model")
    cfg.extra_fields = {
        "shell_sandbox": {
            "mode": mode,
            "backend": backend,
            "network": network,
            "preview_access": preview_access,
            "docker_image": docker_image,
            "clear_env": clear_env,
            "docker_pids_limit": docker_pids_limit,
            "docker_memory": docker_memory,
            "docker_cpus": docker_cpus,
            "docker_read_only": docker_read_only,
            "protect_repo_meta": protect_repo_meta,
            "docker_env_allowlist": docker_env_allowlist,
        }
    }
    return cfg


def _plain_cfg() -> AppConfig:
    cfg = AppConfig(model="test-model")
    cfg.extra_fields = {}
    return cfg


def _has_argv_triple(argv: list[str], first: str, second: str, third: str) -> bool:
    for idx in range(len(argv) - 2):
        if argv[idx] == first and argv[idx + 1] == second and argv[idx + 2] == third:
            return True
    return False


def _argv_triple_index(argv: list[str], first: str, second: str, third: str) -> int:
    for idx in range(len(argv) - 2):
        if argv[idx] == first and argv[idx + 1] == second and argv[idx + 2] == third:
            return idx
    raise AssertionError(f"missing argv triple: {first} {second} {third}")


def _same_path_arg(actual: str, expected: str) -> bool:
    return os.path.normcase(os.path.normpath(actual)) == os.path.normcase(
        os.path.normpath(expected)
    )


def _path_arg_is_relative_to(path: Path, root: Path) -> bool:
    path_s = os.path.normcase(os.path.normpath(os.fspath(path)))
    root_s = os.path.normcase(os.path.normpath(os.fspath(root)))
    return path_s == root_s or path_s.startswith(root_s + os.sep)


def _raw_symlink_target(path: Path) -> Path | None:
    try:
        raw = os.readlink(path)
    except OSError:
        return None
    target = Path(raw)
    if not target.is_absolute():
        target = path.parent / target
    return target


def _same_bwrap_dest_arg(actual: str, expected: str) -> bool:
    return actual.replace("\\", "/") == expected.replace("\\", "/")


def _bwrap_ro_bind_index(argv: list[str], host: str, dest: str) -> int:
    for idx in range(len(argv) - 2):
        if (
            argv[idx] == "--ro-bind"
            and _same_path_arg(argv[idx + 1], host)
            and _same_bwrap_dest_arg(argv[idx + 2], dest)
        ):
            return idx
    raise AssertionError(f"missing argv ro-bind: {host} {dest}")


def _has_bwrap_ro_bind(argv: list[str], host: str, dest: str) -> bool:
    for idx in range(len(argv) - 2):
        if (
            argv[idx] == "--ro-bind"
            and _same_path_arg(argv[idx + 1], host)
            and _same_bwrap_dest_arg(argv[idx + 2], dest)
        ):
            return True
    return False


def _bwrap_dest(path: Path) -> str:
    return PurePosixPath(path.resolve().as_posix()).as_posix()


def _argv_setenv_value(argv: list[str], key: str) -> str:
    for idx in range(len(argv) - 2):
        if argv[idx] == "--setenv" and argv[idx + 1] == key:
            return argv[idx + 2]
    raise AssertionError(f"missing --setenv {key}")


def _create_dir_symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlink creation is not supported on this platform")


def _patch_successful_docker_popen(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, object],
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> None:
    class FakePopen:
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            captured["args"] = args
            captured["kwargs"] = kwargs
            self.returncode: int | None = None

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            captured["communicate_timeout"] = timeout
            self.returncode = returncode
            return stdout, stderr

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            captured["killed"] = True
            self.returncode = -9

        def wait(self, timeout: int | None = None) -> int:
            captured["wait_timeout"] = timeout
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    monkeypatch.setattr(sandbox_runner_mod.subprocess, "Popen", FakePopen)


def _patch_timeout_docker_popen(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, object],
) -> None:
    class FakePopen:
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            captured["args"] = args
            captured["kwargs"] = kwargs
            self.args = args[0]
            self.returncode: int | None = None

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            captured["communicate_timeout"] = timeout
            raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout)

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            captured["killed"] = True
            self.returncode = -9

        def wait(self, timeout: int | None = None) -> int:
            captured["wait_timeout"] = timeout
            if self.returncode is None:
                self.returncode = -9
            return self.returncode

    monkeypatch.setattr(sandbox_runner_mod.subprocess, "Popen", FakePopen)


def _patch_docker_cleanup_run(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_calls: list[list[str]],
) -> None:
    def fake_run(args, **_kwargs):  # type: ignore[no-untyped-def]
        cleanup_calls.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox_runner_mod.subprocess, "run", fake_run)


def test_resolve_shell_sandbox_settings_env_overrides_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(
        mode="warn",
        backend="docker",
        network="on",
        preview_access="local",
        clear_env=False,
        docker_pids_limit=128,
        docker_memory="512m",
        docker_cpus="1",
        docker_read_only=False,
        docker_env_allowlist=["LANG"],
    )
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "strict")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_BACKEND", "bwrap")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_NETWORK", "off")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_PREVIEW_ACCESS", "lan")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_DOCKER_IMAGE", "sandbox:test")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_CLEAR_ENV", "1")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_DOCKER_PIDS_LIMIT", "256")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_DOCKER_MEMORY", "1g")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_DOCKER_CPUS", "1.5")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_DOCKER_READ_ONLY", "1")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_PROTECT_REPO_META", "1")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_DOCKER_ENV_ALLOWLIST", "LANG,GIT_AUTHOR_NAME")

    settings = resolve_shell_sandbox_settings(cfg)
    assert settings.mode == "strict"
    assert settings.backend == "bwrap"
    assert settings.network == "off"
    assert settings.preview_access == "lan"
    assert settings.docker_image == "sandbox:test"
    assert settings.clear_env is True
    assert settings.docker_pids_limit == 256
    assert settings.docker_memory == "1g"
    assert settings.docker_cpus == "1.5"
    assert settings.docker_read_only is True
    assert settings.protect_repo_meta is True
    assert settings.docker_env_allowlist == ("LANG", "GIT_AUTHOR_NAME")


def test_resolve_shell_sandbox_settings_defaults_from_plain_app_config() -> None:
    settings = resolve_shell_sandbox_settings(_plain_cfg())
    assert settings.mode == "strict"
    import platform

    assert settings.backend == ("windows-mxc" if platform.system() == "Windows" else "auto")
    assert settings.network == "off"
    assert settings.preview_access == "auto"
    assert settings.bwrap_profile == "hardened"
    assert settings.clear_env is True
    assert settings.protect_repo_meta is True


def test_resolve_shell_sandbox_settings_invalid_value_raises() -> None:
    cfg = _cfg(mode="invalid")
    with pytest.raises(ConfigError, match="shell_sandbox.mode"):
        resolve_shell_sandbox_settings(cfg)


def test_resolve_shell_sandbox_settings_invalid_preview_access_raises() -> None:
    cfg = _cfg(preview_access="internet")
    with pytest.raises(ConfigError, match="shell_sandbox.preview_access"):
        resolve_shell_sandbox_settings(cfg)


def test_resolve_shell_sandbox_settings_invalid_env_value_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg()
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_NETWORK", "invalid")
    with pytest.raises(ConfigError, match="ALYSIS_SHELL_SANDBOX_NETWORK"):
        resolve_shell_sandbox_settings(cfg)


def test_resolve_shell_sandbox_settings_parses_docker_hardening_fields() -> None:
    cfg = _cfg(
        mode="warn",
        backend="docker",
        docker_pids_limit=256,
        docker_memory="1g",
        docker_cpus="1.5",
        docker_read_only=True,
        protect_repo_meta=True,
        docker_env_allowlist=["GIT_AUTHOR_NAME", "LANG"],
    )
    settings = resolve_shell_sandbox_settings(cfg)
    assert settings.docker_pids_limit == 256
    assert settings.docker_memory == "1g"
    assert settings.docker_cpus == "1.5"
    assert settings.docker_read_only is True
    assert settings.protect_repo_meta is True
    assert settings.docker_env_allowlist == ("GIT_AUTHOR_NAME", "LANG")


def test_resolve_shell_sandbox_settings_invalid_pids_limit_raises() -> None:
    cfg = _cfg(docker_pids_limit=0)
    with pytest.raises(ConfigError, match="docker_pids_limit"):
        resolve_shell_sandbox_settings(cfg)


def test_build_shell_runner_default_auto_prefers_hardened_bwrap_on_linux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_runner_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        sandbox_runner_mod.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )

    runner = build_shell_runner(_plain_cfg(), tmp_path)
    assert isinstance(runner, BwrapShellRunner)
    assert runner.network == "off"
    assert runner.clear_env is True
    assert runner.profile == "hardened"


def test_build_shell_runner_default_auto_uses_hardened_docker_when_bwrap_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_runner_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        sandbox_runner_mod.shutil,
        "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )

    runner = build_shell_runner(_plain_cfg(), tmp_path)
    assert isinstance(runner, DockerShellRunner)
    assert runner.network == "off"
    assert runner.clear_env is True
    assert runner.protect_repo_meta is True


def test_build_shell_runner_passes_docker_hardening_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_runner_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        sandbox_runner_mod.shutil,
        "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    warnings: list[str] = []
    warning_callback = warnings.append

    runner = build_shell_runner(
        _cfg(
            mode="warn",
            backend="docker",
            docker_pids_limit=300,
            docker_memory="2g",
            docker_cpus="2",
            docker_read_only=True,
            protect_repo_meta=True,
            docker_env_allowlist=["LANG", "GIT_SSH_COMMAND"],
        ),
        tmp_path,
        warning_callback=warning_callback,
    )
    assert isinstance(runner, DockerShellRunner)
    assert runner.pids_limit == 300
    assert runner.memory_limit == "2g"
    assert runner.cpus == "2"
    assert runner.read_only_rootfs is True
    assert runner.protect_repo_meta is True
    assert runner.env_allowlist == ("LANG", "GIT_SSH_COMMAND")
    assert runner.warning_callback is warning_callback


def test_build_shell_runner_warn_mode_returns_disabled_runner_when_no_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_runner_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox_runner_mod.shutil, "which", lambda _name: None)

    runner = build_shell_runner(_cfg(mode="warn", backend="auto"), tmp_path)
    assert isinstance(runner, DisabledShellRunner)
    assert "host fallback is disabled" in runner.reason


def test_build_shell_runner_default_strict_mode_errors_when_no_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_runner_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox_runner_mod.shutil, "which", lambda _name: None)

    with pytest.raises(ConfigError, match="strict mode"):
        build_shell_runner(_plain_cfg(), tmp_path)


def test_create_session_defers_strict_shell_runner_construction_until_shell_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0

    def fail_build_shell_runner(**_kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        raise ConfigError(
            "Shell sandbox strict mode is enabled, but no usable backend is available: "
            "auto backend could not find bwrap or docker. Install bubblewrap (Linux) or "
            "Docker, or set ALYSIS_SHELL_SANDBOX_MODE=off for explicit unsafe host execution."
        )

    monkeypatch.setattr(agent_loop_mod, "build_shell_runner", fail_build_shell_runner)

    session = create_session(
        cfg=_cfg(mode="strict", backend="docker"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        non_interactive=True,
    )
    try:
        assert call_count == 0
        with pytest.raises(shell_mod.ShellError, match="strict mode is enabled"):
            session.tools["shell_run"].run({"cmd": "echo hi"})
        assert call_count == 1
    finally:
        session.close()


def test_build_shell_runner_off_mode_explicitly_returns_host_runner(tmp_path: Path) -> None:
    runner = build_shell_runner(_cfg(mode="off", backend="auto"), tmp_path)
    assert isinstance(runner, HostShellRunner)


def test_shell_run_without_runner_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(shell_mod.ShellError, match="implicit host execution is disabled"):
        shell_mod.shell_run(root=tmp_path, cmd="echo hi", cwd="nested")


def test_shell_run_with_explicit_host_runner_uses_host_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sandbox_runner_mod.subprocess, "run", fake_run)

    result = shell_mod.shell_run(
        root=tmp_path,
        cmd="echo hi",
        cwd="nested",
        runner=HostShellRunner(),
    )
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is True
    assert kwargs["cwd"] == os.fspath((tmp_path / "nested").resolve())
    assert result["exit_code"] == 0
    assert result["stdout"] == "ok"


def test_shell_run_preserves_denied_command_without_switching_interpreters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    responses = [
        subprocess.CompletedProcess(
            args="python -m unittest -v",
            returncode=127,
            stdout="",
            stderr="/bin/sh: 1: python: Permission denied",
        ),
        subprocess.CompletedProcess(
            args="python3 -m unittest -v",
            returncode=0,
            stdout="ok",
            stderr="",
        ),
    ]

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(str(args[0]))
        return responses[len(calls) - 1]

    monkeypatch.setattr(sandbox_runner_mod.subprocess, "run", fake_run)

    result = shell_mod.shell_run(
        root=tmp_path,
        cmd="python -m unittest -v",
        runner=HostShellRunner(),
    )
    assert calls == ["python -m unittest -v"]
    assert result["cmd"] == "python -m unittest -v"
    assert result["effective_cmd"] == "python -m unittest -v"
    assert result["exit_code"] == 127
    assert result["stderr"] == "/bin/sh: 1: python: Permission denied"


def test_shell_run_with_bwrap_runner_wraps_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sandbox_runner_mod.subprocess, "run", fake_run)

    runner = BwrapShellRunner(network="off", clear_env=False)
    result = shell_mod.shell_run(root=tmp_path, cmd="echo hi", cwd="work", runner=runner)

    argv = list(captured["args"][0])  # type: ignore[index]
    assert argv[0] == "bwrap"
    bind_idx = argv.index("--bind")
    assert argv[bind_idx + 1] == os.fspath(tmp_path.resolve())
    assert argv[bind_idx + 2] == "/workspace"
    chdir_idx = argv.index("--chdir")
    assert argv[chdir_idx + 1] == "/workspace/work"
    assert "--unshare-pid" in argv
    assert "--unshare-net" in argv
    assert argv[-3:] == ["sh", "-lc", "echo hi"]
    assert result["exit_code"] == 0


@pytest.fixture
def bwrap_primary_scope(tmp_path, monkeypatch):
    from alysis_code import host_file_policy

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    config = outside / "configuration"
    data = outside / "data"
    runtime = outside / "runtime"
    for directory in (config, data, runtime):
        directory.mkdir()
    monkeypatch.setattr(host_file_policy.config, "config_path", lambda: config / "config.json")
    monkeypatch.setattr(host_file_policy.config, "_data_dir", lambda: data)
    prefixes = ("prefix", "exec_prefix", "base_prefix", "base_exec_prefix")
    monkeypatch.setattr(
        host_file_policy, "sys", SimpleNamespace(**dict.fromkeys(prefixes, str(runtime)))
    )
    for prefix in ("ALYSIS", "SYLLIPTOR"):
        for name in ("DESKTOP_CONTROL_PATHS", "DESKTOP_USER_DATA", "DESKTOP_RUNTIME_ROOTS"):
            monkeypatch.delenv(f"{prefix}_{name}", raising=False)
    # In particular, no extended-root option may activate the older check.
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", "[]")

    def argv(root, access="workspace-write"):
        return sandbox_runner_mod._build_bwrap_argv(
            root=root,
            cwd=root,
            cmd="never executed",
            network="off",
            clear_env=True,
            profile="compat",
            unshare_cgroup=False,
            filesystem_access=access,
            additional_workspace_roots=(),
        )[0]

    return workspace, host_file_policy, argv


@pytest.mark.parametrize("source", ["configuration", "data", "desktop-control"])
@pytest.mark.parametrize("relation", ["contains-private", "inside-private"])
@pytest.mark.parametrize("access", ["workspace-write", "read-only"])
def test_bwrap_primary_scope_refuses_private_overlap_without_extended_roots(
    bwrap_primary_scope, monkeypatch, source, relation, access
):
    workspace, policy, argv = bwrap_primary_scope
    private = workspace / "private"
    project = private / "project"
    project.mkdir(parents=True)
    canary = private / "canary.txt"
    canary.write_text("synthetic-private")
    if source == "configuration":
        monkeypatch.setattr(policy.config, "config_path", lambda: private / "config.json")
    elif source == "data":
        monkeypatch.setattr(policy.config, "_data_dir", lambda: private)
    else:
        monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(private)]))
    root = workspace if relation == "contains-private" else project
    with pytest.raises(ConfigError, match="private host state"):
        argv(root, access)
    assert canary.read_text() == "synthetic-private"


@pytest.mark.parametrize("prefix", ["prefix", "exec_prefix", "base_prefix", "base_exec_prefix"])
@pytest.mark.parametrize("relation", ["contains-runtime", "inside-runtime"])
def test_bwrap_primary_scope_refuses_python_alias_writes_without_extended_roots(
    bwrap_primary_scope, monkeypatch, prefix, relation
):
    workspace, policy, argv = bwrap_primary_scope
    runtime = workspace / "runtime"
    package = runtime / "lib" / "synthetic_package"
    package.mkdir(parents=True)
    monkeypatch.setattr(policy.sys, prefix, str(runtime))
    root = workspace if relation == "contains-runtime" else package
    with pytest.raises(ConfigError, match="trusted runtime"):
        argv(root)
    # Runtime code is readable; a genuinely read-only primary alias is safe.
    assert _has_argv_triple(argv(root, "read-only"), "--ro-bind", str(root), "/workspace")


def test_bwrap_primary_scope_refuses_future_private_names(bwrap_primary_scope, monkeypatch):
    workspace, _policy, argv = bwrap_primary_scope
    future = workspace / "future-private"
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(future)]))
    with pytest.raises(ConfigError, match="private host state"):
        argv(workspace)
    assert not future.exists()


def test_bwrap_primary_scope_protects_desktop_parent_and_preserves_managed_task(
    bwrap_primary_scope, monkeypatch
):
    workspace, _policy, argv = bwrap_primary_scope
    task = workspace / "work-tasks" / "synthetic-task"
    task.mkdir(parents=True)
    monkeypatch.setenv("ALYSIS_DESKTOP_USER_DATA", str(workspace))
    with pytest.raises(ConfigError, match="application's data folder"):
        argv(workspace)
    assert _has_argv_triple(argv(task), "--bind", str(task), "/workspace")


def test_bwrap_primary_scope_refuses_editable_engine_code(bwrap_primary_scope, monkeypatch):
    workspace, _policy, argv = bwrap_primary_scope
    package = workspace / "src" / "alysis_code"
    package.mkdir(parents=True)
    monkeypatch.setattr(sandbox_runner_mod, "__file__", str(package / "sandbox_runner.py"))
    with pytest.raises(ConfigError, match="trusted runtime"):
        argv(workspace)


def test_bwrap_hardened_runner_adds_read_only_repo_metadata_overlays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    (tmp_path / ".git").mkdir()
    (tmp_path / ".alysis").mkdir()

    def fake_run(*args, **_kwargs):  # type: ignore[no-untyped-def]
        captured["args"] = args
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox_runner_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(sandbox_runner_mod, "_supports_bwrap_unshare_cgroup", lambda: False)

    runner = BwrapShellRunner(network="off", clear_env=False, profile="hardened")
    runner.run(root=tmp_path, cwd=tmp_path, cmd="echo hi", timeout_s=5)

    argv = list(captured["args"][0])  # type: ignore[index]
    git_host = os.fspath((tmp_path / ".git").resolve())
    alysis_host = os.fspath((tmp_path / ".alysis").resolve())
    assert _has_argv_triple(argv, "--ro-bind", git_host, "/workspace/.git")
    assert _has_argv_triple(argv, "--ro-bind", alysis_host, "/workspace/.alysis")


def test_bwrap_hardened_runner_skips_symlinked_repo_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    outside = tmp_path / "outside"
    outside.mkdir()
    _create_dir_symlink_or_skip(tmp_path / ".git", outside)

    def fake_run(*args, **_kwargs):  # type: ignore[no-untyped-def]
        captured["args"] = args
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox_runner_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(sandbox_runner_mod, "_supports_bwrap_unshare_cgroup", lambda: False)

    runner = BwrapShellRunner(network="off", clear_env=False, profile="hardened")
    runner.run(root=tmp_path, cwd=tmp_path, cmd="echo hi", timeout_s=5)

    argv = list(captured["args"][0])  # type: ignore[index]
    outside_host = os.fspath(outside.resolve())
    assert not _has_argv_triple(argv, "--ro-bind", outside_host, "/workspace/.git")


def test_bwrap_hardened_java_config_bind_paths_discovers_openjdk_dirs(
    tmp_path: Path,
) -> None:
    etc = tmp_path / "etc"
    java_dir = etc / "java-17-openjdk"
    dot_java_dir = etc / ".java"
    java_dir.mkdir(parents=True)
    dot_java_dir.mkdir()
    (etc / "java-not-a-dir").write_text("ignored\n", encoding="utf-8")

    paths = sandbox_runner_mod._hardened_java_config_ro_bind_paths(etc_root=etc)

    assert paths == (dot_java_dir.resolve(), java_dir.resolve())


def test_bwrap_hardened_runner_binds_java_config_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **_kwargs):  # type: ignore[no-untyped-def]
        captured["args"] = args
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox_runner_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(sandbox_runner_mod, "_supports_bwrap_unshare_cgroup", lambda: False)
    monkeypatch.setattr(sandbox_runner_mod.shutil, "which", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sandbox_runner_mod,
        "_hardened_java_config_ro_bind_paths",
        lambda: (Path("/etc/java-17-openjdk"),),
    )

    runner = BwrapShellRunner(network="off", clear_env=False, profile="hardened")
    runner.run(root=tmp_path, cwd=tmp_path, cmd="javac src/Main.java", timeout_s=5)

    argv = list(captured["args"][0])  # type: ignore[index]
    assert _has_argv_triple(
        argv,
        "--ro-bind",
        "/etc/java-17-openjdk",
        "/etc/java-17-openjdk",
    )


def test_bwrap_runner_binds_active_python_runtime_after_tmpfs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    venv_root = tmp_path / "runtime" / "venv"
    venv_bin = venv_root / "bin"
    venv_bin.mkdir(parents=True)
    cpython_root = tmp_path / "runtime" / "cpython-3.12.13"
    cpython_bin = cpython_root / "bin"
    cpython_bin.mkdir(parents=True)
    cpython_python = cpython_bin / "python3.12"
    cpython_python.write_text("#!/bin/sh\n", encoding="utf-8")
    uv_python_root = tmp_path / "runtime" / "uv" / "python"
    uv_python_root.mkdir(parents=True)
    cpython_alias = uv_python_root / "cpython-3.12"
    shim_dir = tmp_path / "home" / "apollo" / ".local" / "bin"
    shim_dir.mkdir(parents=True)
    shim_python = shim_dir / "python3.12"
    venv_python = venv_bin / "python"
    venv_python312 = venv_bin / "python3.12"
    try:
        cpython_alias.symlink_to(cpython_root, target_is_directory=True)
        shim_python.symlink_to(cpython_alias / "bin" / "python3.12")
        venv_python.symlink_to("python3.12")
        venv_python312.symlink_to(shim_python)
    except (OSError, NotImplementedError):
        pytest.skip("file symlink creation is not supported on this platform")

    def fake_run(*args, **_kwargs):  # type: ignore[no-untyped-def]
        captured["args"] = args
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox_runner_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(sandbox_runner_mod, "_supports_bwrap_unshare_cgroup", lambda: False)
    monkeypatch.setattr(sandbox_runner_mod.sys, "prefix", os.fspath(venv_root))
    monkeypatch.setattr(sandbox_runner_mod.sys, "exec_prefix", os.fspath(venv_root))
    monkeypatch.setattr(sandbox_runner_mod.sys, "base_prefix", os.fspath(cpython_root))
    monkeypatch.setattr(sandbox_runner_mod.sys, "base_exec_prefix", os.fspath(cpython_root))
    monkeypatch.setattr(sandbox_runner_mod.sys, "executable", os.fspath(venv_python))
    monkeypatch.setattr(sandbox_runner_mod.shutil, "which", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/local/bin", "/usr/bin"]))

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = BwrapShellRunner(network="off", clear_env=False, profile="hardened")
    runner.run(root=workspace, cwd=workspace, cmd="python -m pytest -q", timeout_s=5)

    argv = list(captured["args"][0])  # type: ignore[index]
    venv_s = os.fspath(venv_root.resolve())
    venv_bin_s = os.fspath(venv_bin.resolve())
    cpython_s = os.fspath(cpython_root.resolve())
    shim_dir_s = os.fspath(shim_dir.resolve())
    uv_python_s = os.fspath(uv_python_root.resolve())
    cpython_python_s = os.fspath(cpython_python.resolve())
    tmpfs_idx = argv.index("--tmpfs")
    assert argv[tmpfs_idx + 1] == "/tmp"
    expected_bound_roots = [venv_s, cpython_s]
    venv_python312_target = _raw_symlink_target(venv_python312)
    if venv_python312_target is not None and _path_arg_is_relative_to(
        venv_python312_target, shim_dir
    ):
        expected_bound_roots.append(shim_dir_s)
    shim_python_target = _raw_symlink_target(shim_python)
    if shim_python_target is not None and _path_arg_is_relative_to(
        shim_python_target, uv_python_root
    ):
        expected_bound_roots.append(uv_python_s)
    for bound_root in expected_bound_roots:
        bind_idx = _bwrap_ro_bind_index(argv, bound_root, _bwrap_dest(Path(bound_root)))
        assert bind_idx > tmpfs_idx
    assert not _has_bwrap_ro_bind(argv, venv_bin_s, _bwrap_dest(venv_bin))
    assert not _has_bwrap_ro_bind(argv, cpython_python_s, _bwrap_dest(venv_python312))
    assert not _has_bwrap_ro_bind(argv, cpython_python_s, _bwrap_dest(shim_python))
    assert not _has_bwrap_ro_bind(
        argv,
        cpython_python_s,
        _bwrap_dest(cpython_alias / "bin" / "python3.12"),
    )
    path_entries = _argv_setenv_value(argv, "PATH").split(os.pathsep)
    assert path_entries[:2] == ["/usr/local/bin", "/usr/bin"]
    assert venv_bin_s not in path_entries


def test_shell_run_with_docker_runner_wraps_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    _patch_successful_docker_popen(monkeypatch, captured, stdout="ok")

    runner = DockerShellRunner(
        network="off",
        docker_image=TEST_DOCKER_IMAGE,
        clear_env=False,
    )
    shell_mod.shell_run(root=tmp_path, cmd="echo hi", cwd="work", runner=runner)

    argv = list(captured["args"][0])  # type: ignore[index]
    assert argv[0] == "docker"
    assert argv[1] == "run"
    assert TEST_DOCKER_IMAGE in argv
    assert "--init" in argv
    name_idx = argv.index("--name")
    assert re.fullmatch(r"alysis-sbx-[0-9a-f]{12}", argv[name_idx + 1])
    mount = f"{os.fspath(tmp_path.resolve())}:/workspace:rw"
    assert "-v" in argv
    assert mount in argv
    network_idx = argv.index("--network")
    assert argv[network_idx + 1] == "none"
    workdir_idx = argv.index("-w")
    assert argv[workdir_idx + 1] == "/workspace/work"
    assert argv[-3] == "sh"
    assert argv[-2] == "-lc"
    assert argv[-1] == "mkdir -p /tmp/home && echo hi"


def test_docker_runner_uses_init_and_unique_container_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    _patch_successful_docker_popen(monkeypatch, captured)

    runner = DockerShellRunner(network="off", docker_image=TEST_DOCKER_IMAGE)
    runner.run(root=tmp_path, cwd=tmp_path, cmd="echo hi", timeout_s=5)

    argv = list(captured["args"][0])  # type: ignore[index]
    assert "--init" in argv
    name_idx = argv.index("--name")
    assert re.fullmatch(r"alysis-sbx-[0-9a-f]{12}", argv[name_idx + 1])


def test_docker_runner_timeout_kills_and_removes_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    cleanup_calls: list[list[str]] = []
    warnings: list[str] = []
    _patch_timeout_docker_popen(monkeypatch, captured)
    _patch_docker_cleanup_run(monkeypatch, cleanup_calls)

    runner = DockerShellRunner(
        network="off",
        docker_image=TEST_DOCKER_IMAGE,
        warning_callback=warnings.append,
    )
    with pytest.raises(shell_mod.ShellError, match="Command timed out after 2s"):
        shell_mod.shell_run(root=tmp_path, cmd="sleep 999", timeout_s=2, runner=runner)

    argv = list(captured["args"][0])  # type: ignore[index]
    name = argv[argv.index("--name") + 1]
    assert ["docker", "kill", "--signal=KILL", name] in cleanup_calls
    assert ["docker", "rm", "-f", name] in cleanup_calls
    assert captured["killed"] is True
    assert captured["wait_timeout"] == 5


def test_docker_runner_timeout_emits_warning_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    cleanup_calls: list[list[str]] = []
    warnings: list[str] = []
    _patch_timeout_docker_popen(monkeypatch, captured)
    _patch_docker_cleanup_run(monkeypatch, cleanup_calls)

    runner = DockerShellRunner(
        network="off",
        docker_image=TEST_DOCKER_IMAGE,
        warning_callback=warnings.append,
    )
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run(root=tmp_path, cwd=tmp_path, cmd="sleep 999", timeout_s=2)

    assert cleanup_calls
    assert any("timeout" in warning.lower() or "kill" in warning.lower() for warning in warnings)


def test_docker_runner_adds_repo_metadata_read_only_mounts_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    (tmp_path / ".git").mkdir()

    _patch_successful_docker_popen(monkeypatch, captured)

    runner = DockerShellRunner(
        network="off",
        docker_image=TEST_DOCKER_IMAGE,
        clear_env=False,
        protect_repo_meta=True,
    )
    runner.run(root=tmp_path, cwd=tmp_path, cmd="echo hi", timeout_s=5)

    argv = list(captured["args"][0])  # type: ignore[index]
    ro_mount = f"{os.fspath((tmp_path / '.git').resolve())}:/workspace/.git:ro"
    assert ro_mount in argv


def test_docker_runner_skips_symlinked_repo_metadata_mounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    outside = tmp_path / "outside"
    outside.mkdir()
    _create_dir_symlink_or_skip(tmp_path / ".git", outside)

    _patch_successful_docker_popen(monkeypatch, captured)

    runner = DockerShellRunner(
        network="off",
        docker_image=TEST_DOCKER_IMAGE,
        clear_env=False,
        protect_repo_meta=True,
    )
    runner.run(root=tmp_path, cwd=tmp_path, cmd="echo hi", timeout_s=5)

    argv = list(captured["args"][0])  # type: ignore[index]
    outside_mount = f"{os.fspath(outside.resolve())}:/workspace/.git:ro"
    assert outside_mount not in argv


def test_docker_runner_does_not_forward_host_path_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    _patch_successful_docker_popen(monkeypatch, captured)
    monkeypatch.setenv("PATH", "__HOST_PATH_SENTINEL__")
    monkeypatch.setenv("HOME", "__HOST_HOME_SENTINEL__")
    monkeypatch.setenv("LANG", "en_US.UTF-8")

    runner = DockerShellRunner(network="off", clear_env=False)
    runner.run(root=tmp_path, cwd=tmp_path, cmd="echo hi", timeout_s=5)

    argv = list(captured["args"][0])  # type: ignore[index]
    e_values: list[str] = []
    for idx, arg in enumerate(argv[:-1]):
        if arg == "-e":
            e_values.append(argv[idx + 1])
    keys = {item.split("=", 1)[0].upper() for item in e_values if "=" in item}
    joined = " ".join(e_values)

    assert "PATH" not in keys
    assert "__HOST_PATH_SENTINEL__" not in joined
    assert "__HOST_HOME_SENTINEL__" not in joined
    assert "HOME=/tmp/home" in e_values


def test_docker_runner_prefixes_command_with_home_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    _patch_successful_docker_popen(monkeypatch, captured)
    runner = DockerShellRunner(network="off", clear_env=False)
    runner.run(root=tmp_path, cwd=tmp_path, cmd="echo hi", timeout_s=5)

    argv = list(captured["args"][0])  # type: ignore[index]
    assert argv[-3] == "sh"
    assert argv[-2] == "-lc"
    assert argv[-1].startswith("mkdir -p /tmp/home && ")
    assert argv[-1].endswith("echo hi")


def test_docker_runner_applies_runtime_limits_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    _patch_successful_docker_popen(monkeypatch, captured)
    runner = DockerShellRunner(
        network="off",
        clear_env=False,
        pids_limit=256,
        memory_limit="1g",
        cpus="1.25",
        read_only_rootfs=True,
    )
    runner.run(root=tmp_path, cwd=tmp_path, cmd="echo hi", timeout_s=5)

    argv = list(captured["args"][0])  # type: ignore[index]
    pids_idx = argv.index("--pids-limit")
    assert argv[pids_idx + 1] == "256"
    memory_idx = argv.index("--memory")
    assert argv[memory_idx + 1] == "1g"
    cpus_idx = argv.index("--cpus")
    assert argv[cpus_idx + 1] == "1.25"
    assert "--read-only" in argv
    tmpfs_idx = argv.index("--tmpfs")
    assert argv[tmpfs_idx + 1] == "/tmp:rw,exec,nosuid,nodev"


def test_docker_runner_allowlist_filters_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    _patch_successful_docker_popen(monkeypatch, captured)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Alysis Code")
    monkeypatch.setenv("SHOULD_DROP_ME", "1")
    runner = DockerShellRunner(
        network="off",
        clear_env=False,
        env_allowlist=("GIT_AUTHOR_NAME",),
    )
    runner.run(root=tmp_path, cwd=tmp_path, cmd="echo hi", timeout_s=5)

    argv = list(captured["args"][0])  # type: ignore[index]
    e_values: list[str] = []
    for idx, arg in enumerate(argv[:-1]):
        if arg == "-e":
            e_values.append(argv[idx + 1])
    keys = {item.split("=", 1)[0].upper() for item in e_values if "=" in item}

    assert "GIT_AUTHOR_NAME" in keys
    assert "LANG" in keys
    assert "SHOULD_DROP_ME" not in keys
    assert "HOME=/tmp/home" in e_values


def test_bwrap_runner_strips_sensitive_env_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox_runner_mod.subprocess, "run", fake_run)
    monkeypatch.setenv("ALYSIS_API_KEY", "secret-alysis")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-openai")
    monkeypatch.setenv("KEEP_ME", "1")

    runner = BwrapShellRunner(network="off", clear_env=False)
    runner.run(root=tmp_path, cwd=tmp_path, cmd="echo hi", timeout_s=5)

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert "ALYSIS_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["KEEP_ME"] == "1"


def test_bwrap_runner_clear_env_uses_minimal_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox_runner_mod.subprocess, "run", fake_run)
    monkeypatch.setenv("ALYSIS_API_KEY", "secret-alysis")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-openai")
    monkeypatch.setenv("EXTRA_KEY", "value")

    runner = BwrapShellRunner(network="off", clear_env=True)
    runner.run(root=tmp_path, cwd=tmp_path, cmd="echo hi", timeout_s=5)

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["HOME"] == "/tmp/home"
    assert set(env).issubset(
        {"HOME", "PATH", "LANG", "DEBIAN_FRONTEND", "GIT_TERMINAL_PROMPT", "PIP_NO_INPUT"}
    )
    assert env["DEBIAN_FRONTEND"] == "noninteractive"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["PIP_NO_INPUT"] == "1"
    assert "ALYSIS_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "EXTRA_KEY" not in env
