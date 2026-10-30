from __future__ import annotations

import ipaddress
import os
import subprocess
from pathlib import Path

import pytest

import alysis_code.background_runner as background_runner_mod
import alysis_code.durable_service_manager as durable_service_manager_mod
import alysis_code.sandbox_runner as sandbox_runner_mod
from alysis_code.background_runner import (
    BwrapBackgroundRunner,
    DisabledBackgroundRunner,
    DockerBackgroundRunner,
    HostBackgroundRunner,
    build_background_shell_runner,
)
from alysis_code.config import AppConfig, ConfigError
from alysis_code.durable_service_manager import DurableServiceManager
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.service_persistence import apply_non_interactive_defaults

TEST_DOCKER_IMAGE = "test/alysis-sandbox:dev"


@pytest.fixture(autouse=True)
def _disable_real_bwrap_cgroup_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(background_runner_mod, "_supports_bwrap_unshare_cgroup", lambda: False)


class FakePopen:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs
        self.pid = 12345
        self.stdout = None
        self.stderr = None
        self.returncode = None

    def poll(self) -> int | None:
        return self.returncode


def _cfg(
    *,
    mode: str = "off",
    backend: str = "auto",
    network: str = "off",
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


def _patch_popen(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, object]]) -> None:
    def fake_popen(*args: object, **kwargs: object) -> FakePopen:
        calls.append({"args": args, "kwargs": kwargs})
        return FakePopen(*args, **kwargs)

    monkeypatch.setattr(background_runner_mod.subprocess, "Popen", fake_popen)


def _argv_from_call(call: dict[str, object]) -> list[str]:
    args = call["args"]
    assert isinstance(args, tuple)
    return list(args[0])  # type: ignore[arg-type]


def _kwargs_from_call(call: dict[str, object]) -> dict[str, object]:
    kwargs = call["kwargs"]
    assert isinstance(kwargs, dict)
    return kwargs


def _container_name(argv: list[str]) -> str:
    name_idx = argv.index("--name")
    return argv[name_idx + 1]


def test_bwrap_background_runner_uses_extracted_argv_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    _patch_popen(monkeypatch, calls)
    monkeypatch.setattr(
        background_runner_mod,
        "_build_bwrap_argv",
        lambda **_kwargs: (["bwrap", "sentinel"], {"BASE": "1"}),
    )

    spawn = BwrapBackgroundRunner().start(root=tmp_path, cwd=tmp_path, cmd="echo hi")

    assert _argv_from_call(calls[0]) == ["bwrap", "sentinel"]
    assert _kwargs_from_call(calls[0])["env"] == apply_non_interactive_defaults({"BASE": "1"})
    assert spawn.started_argv == ("bwrap", "sentinel")


def test_bwrap_background_runner_uses_devnull_pipe_pipe_streams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("bwrap background runner is Linux-only at the factory boundary")
    calls: list[dict[str, object]] = []
    _patch_popen(monkeypatch, calls)
    monkeypatch.setattr(
        background_runner_mod,
        "_build_bwrap_argv",
        lambda **_kwargs: (["bwrap", "sentinel"], {}),
    )

    BwrapBackgroundRunner().start(root=tmp_path, cwd=tmp_path, cmd="echo hi")

    kwargs = _kwargs_from_call(calls[0])
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["bufsize"] == 0
    assert kwargs["start_new_session"] is True


def test_bwrap_background_runner_filters_sensitive_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    _patch_popen(monkeypatch, calls)
    monkeypatch.setattr(
        background_runner_mod,
        "_build_bwrap_argv",
        lambda **_kwargs: (["bwrap", "sentinel"], {"BASE": "1"}),
    )

    BwrapBackgroundRunner().start(
        root=tmp_path,
        cwd=tmp_path,
        cmd="echo hi",
        env_overrides={"ALYSIS_API_KEY": "x", "SAFE": "y"},
    )

    env = _kwargs_from_call(calls[0])["env"]
    assert isinstance(env, dict)
    assert "ALYSIS_API_KEY" not in env
    assert env["SAFE"] == "y"
    assert env["BASE"] == "1"


def test_bwrap_background_runner_cleanup_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    _patch_popen(monkeypatch, calls)
    monkeypatch.setattr(
        background_runner_mod,
        "_build_bwrap_argv",
        lambda **_kwargs: (["bwrap", "sentinel"], {}),
    )

    spawn = BwrapBackgroundRunner().start(root=tmp_path, cwd=tmp_path, cmd="echo hi")

    spawn.cleanup()


def test_bwrap_background_runner_uses_process_group_termination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    _patch_popen(monkeypatch, calls)
    monkeypatch.setattr(
        background_runner_mod,
        "_build_bwrap_argv",
        lambda **_kwargs: (["bwrap", "sentinel"], {}),
    )

    spawn = BwrapBackgroundRunner().start(root=tmp_path, cwd=tmp_path, cmd="echo hi")

    assert spawn.termination_mode == "process_group"


def test_durable_service_bwrap_launch_removes_die_with_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(durable_service_manager_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        durable_service_manager_mod.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    monkeypatch.setattr(
        durable_service_manager_mod,
        "_build_bwrap_argv",
        lambda **_kwargs: (["bwrap", "--die-with-parent", "sentinel"], {"BASE": "1"}),
    )
    manager = DurableServiceManager(
        root=tmp_path,
        state_dir=tmp_path / "services",
        settings=ShellSandboxSettings(mode="strict", backend="bwrap"),
    )

    launch = manager._build_launch(cmd="echo hi", cwd=tmp_path, service_id="svc_test")

    assert launch["popen_args"] == ["bwrap", "sentinel"]
    assert launch["env"] == {"BASE": "1"}


def test_durable_service_docker_tcp_requires_explicit_networking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(durable_service_manager_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        durable_service_manager_mod.shutil,
        "which",
        lambda name: "/usr/local/bin/docker" if name == "docker" else None,
    )
    manager = DurableServiceManager(
        root=tmp_path,
        state_dir=tmp_path / "services",
        settings=ShellSandboxSettings(mode="strict", backend="docker", network="off"),
    )

    with pytest.raises(RuntimeError, match="workspace_preview_start"):
        manager._build_launch(
            cmd="python -m http.server 4173",
            cwd=tmp_path,
            service_id="svc_test",
            readiness={"type": "tcp", "host": "127.0.0.1", "port": 4173},
        )


def test_durable_service_docker_tcp_publishes_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(durable_service_manager_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        durable_service_manager_mod.shutil,
        "which",
        lambda name: "/usr/local/bin/docker" if name == "docker" else None,
    )

    def fake_build_docker_argv(**kwargs: object) -> tuple[list[str], dict[str, str]]:
        captured.update(kwargs)
        return ["docker", "sentinel"], {}

    monkeypatch.setattr(
        durable_service_manager_mod,
        "_build_docker_argv",
        fake_build_docker_argv,
    )
    manager = DurableServiceManager(
        root=tmp_path,
        state_dir=tmp_path / "services",
        settings=ShellSandboxSettings(mode="strict", backend="docker", network="on"),
    )

    launch = manager._build_launch(
        cmd="python -m http.server 4173 --bind 0.0.0.0",
        cwd=tmp_path,
        service_id="svc_test",
        readiness={"type": "tcp", "host": "127.0.0.1", "port": 4173},
    )

    assert launch["backend"] == "docker"
    published_ports = captured["published_ports"]
    assert isinstance(published_ports, tuple)
    assert len(published_ports) == 1
    publish_host, host_port, container_port = published_ports[0]
    assert ipaddress.ip_address(publish_host).is_loopback
    assert (host_port, container_port) == (4173, 4173)


def test_bwrap_background_runner_resolves_cgroup_support_outside_argv_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    helper_calls: list[dict[str, object]] = []
    _patch_popen(monkeypatch, calls)
    monkeypatch.setattr(background_runner_mod, "_supports_bwrap_unshare_cgroup", lambda: True)

    def fake_build_bwrap_argv(**kwargs: object) -> tuple[list[str], dict[str, str]]:
        helper_calls.append(kwargs)
        return ["bwrap", "sentinel"], {}

    monkeypatch.setattr(background_runner_mod, "_build_bwrap_argv", fake_build_bwrap_argv)

    BwrapBackgroundRunner().start(root=tmp_path, cwd=tmp_path, cmd="echo hi")

    assert helper_calls[0]["unshare_cgroup"] is True


def test_docker_background_runner_uses_extracted_argv_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    helper_calls: list[dict[str, object]] = []
    _patch_popen(monkeypatch, calls)

    def fake_build_docker_argv(**kwargs: object) -> tuple[list[str], dict[str, str]]:
        helper_calls.append(kwargs)
        return ["docker", "sentinel"], {"BASE": "1"}

    monkeypatch.setattr(background_runner_mod, "_build_docker_argv", fake_build_docker_argv)

    spawn = DockerBackgroundRunner().start(root=tmp_path, cwd=tmp_path, cmd="echo hi")

    assert _argv_from_call(calls[0]) == ["docker", "sentinel"]
    assert _kwargs_from_call(calls[0])["env"] == apply_non_interactive_defaults({"BASE": "1"})
    assert spawn.started_argv == ("docker", "sentinel")
    assert str(helper_calls[0]["container_name"]).startswith("alysis-bgsbx-")


def test_docker_background_runner_container_name_uses_bgsbx_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    _patch_popen(monkeypatch, calls)

    DockerBackgroundRunner(docker_image=TEST_DOCKER_IMAGE).start(
        root=tmp_path,
        cwd=tmp_path,
        cmd="echo hi",
    )

    assert _container_name(_argv_from_call(calls[0])).startswith("alysis-bgsbx-")


def test_docker_background_runner_unique_container_name_per_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    _patch_popen(monkeypatch, calls)
    runner = DockerBackgroundRunner(docker_image=TEST_DOCKER_IMAGE)

    runner.start(root=tmp_path, cwd=tmp_path, cmd="echo hi")
    runner.start(root=tmp_path, cwd=tmp_path, cmd="echo hi")

    names = [_container_name(_argv_from_call(call)) for call in calls]
    assert names[0] != names[1]


def test_docker_background_runner_uses_devnull_pipe_pipe_streams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    _patch_popen(monkeypatch, calls)

    DockerBackgroundRunner(docker_image=TEST_DOCKER_IMAGE).start(
        root=tmp_path,
        cwd=tmp_path,
        cmd="echo hi",
    )

    kwargs = _kwargs_from_call(calls[0])
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["bufsize"] == 0
    assert "start_new_session" not in kwargs


def test_docker_background_runner_filters_sensitive_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    _patch_popen(monkeypatch, calls)

    DockerBackgroundRunner(docker_image=TEST_DOCKER_IMAGE).start(
        root=tmp_path,
        cwd=tmp_path,
        cmd="echo hi",
        env_overrides={"ALYSIS_API_KEY": "x", "SAFE": "y"},
    )

    env = _kwargs_from_call(calls[0])["env"]
    assert isinstance(env, dict)
    assert "ALYSIS_API_KEY" not in env
    assert env["SAFE"] == "y"


def test_docker_background_runner_cleanup_kills_and_removes_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    cleanup_calls: list[list[str]] = []
    warnings: list[str] = []
    _patch_popen(monkeypatch, calls)

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        cleanup_calls.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox_runner_mod.subprocess, "run", fake_run)
    spawn = DockerBackgroundRunner(
        docker_image=TEST_DOCKER_IMAGE,
        warning_callback=warnings.append,
    ).start(root=tmp_path, cwd=tmp_path, cmd="echo hi")
    name = _container_name(_argv_from_call(calls[0]))

    spawn.cleanup()

    assert cleanup_calls == [
        ["docker", "kill", "--signal=KILL", name],
        ["docker", "rm", "-f", name],
    ]
    assert warnings == []


def test_docker_cleanup_container_emits_warning_when_not_quiet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cleanup_calls: list[list[str]] = []
    warnings: list[str] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        cleanup_calls.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox_runner_mod.subprocess, "run", fake_run)

    sandbox_runner_mod._docker_cleanup_container(
        "alysis-sbx-test",
        cwd=str(tmp_path),
        env={},
        warning_callback=warnings.append,
        reason="timeout",
    )

    assert len(warnings) == 1
    assert "killing container" in warnings[0]
    assert cleanup_calls == [
        ["docker", "kill", "--signal=KILL", "alysis-sbx-test"],
        ["docker", "rm", "-f", "alysis-sbx-test"],
    ]


def test_docker_cleanup_container_silent_when_quiet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cleanup_calls: list[list[str]] = []
    warnings: list[str] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        cleanup_calls.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox_runner_mod.subprocess, "run", fake_run)

    sandbox_runner_mod._docker_cleanup_container(
        "alysis-bgsbx-test",
        cwd=str(tmp_path),
        env={},
        warning_callback=warnings.append,
        reason="background process cleanup",
        quiet=True,
    )

    assert warnings == []
    assert cleanup_calls == [
        ["docker", "kill", "--signal=KILL", "alysis-bgsbx-test"],
        ["docker", "rm", "-f", "alysis-bgsbx-test"],
    ]


def test_docker_background_runner_cleanup_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    cleanup_calls: list[list[str]] = []
    _patch_popen(monkeypatch, calls)

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        cleanup_calls.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox_runner_mod.subprocess, "run", fake_run)
    spawn = DockerBackgroundRunner(docker_image=TEST_DOCKER_IMAGE).start(
        root=tmp_path,
        cwd=tmp_path,
        cmd="echo hi",
    )

    spawn.cleanup()
    spawn.cleanup()

    assert len(cleanup_calls) == 4


def test_docker_background_runner_uses_direct_termination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    _patch_popen(monkeypatch, calls)

    spawn = DockerBackgroundRunner(docker_image=TEST_DOCKER_IMAGE).start(
        root=tmp_path,
        cwd=tmp_path,
        cmd="echo hi",
    )

    assert spawn.termination_mode == "direct"


def test_build_background_shell_runner_off_mode_returns_host_runner(tmp_path: Path) -> None:
    runner = build_background_shell_runner(_cfg(mode="off", backend="auto"), tmp_path)
    assert isinstance(runner, HostBackgroundRunner)


def test_build_background_shell_runner_default_strict_mode_errors_when_no_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_runner_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox_runner_mod.shutil, "which", lambda _name: None)

    with pytest.raises(ConfigError, match="background processes"):
        build_background_shell_runner(_plain_cfg(), tmp_path)


def test_build_background_shell_runner_warn_mode_returns_disabled_when_no_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(sandbox_runner_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox_runner_mod.shutil, "which", lambda _name: None)

    runner = build_background_shell_runner(
        _cfg(mode="warn", backend="auto"),
        tmp_path,
        warning_callback=warnings.append,
    )

    assert isinstance(runner, DisabledBackgroundRunner)
    assert "host fallback is disabled" in runner.reason
    assert len(warnings) == 1


def test_build_background_shell_runner_auto_prefers_bwrap_on_linux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_runner_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        sandbox_runner_mod.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )

    runner = build_background_shell_runner(_plain_cfg(), tmp_path)

    assert isinstance(runner, BwrapBackgroundRunner)


def test_build_background_shell_runner_auto_uses_docker_when_bwrap_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_runner_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        sandbox_runner_mod.shutil,
        "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )

    runner = build_background_shell_runner(_plain_cfg(), tmp_path)

    assert isinstance(runner, DockerBackgroundRunner)


def test_build_background_shell_runner_explicit_bwrap_errors_when_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_runner_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox_runner_mod.shutil, "which", lambda _name: None)

    with pytest.raises(ConfigError, match="bwrap backend selected"):
        build_background_shell_runner(_cfg(mode="strict", backend="bwrap"), tmp_path)


def test_build_background_shell_runner_explicit_docker_errors_when_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_runner_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox_runner_mod.shutil, "which", lambda _name: None)

    with pytest.raises(ConfigError, match="docker backend selected"):
        build_background_shell_runner(_cfg(mode="strict", backend="docker"), tmp_path)


def test_build_background_shell_runner_passes_docker_hardening_settings_to_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_runner_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        sandbox_runner_mod.shutil,
        "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )

    runner = build_background_shell_runner(
        _cfg(
            mode="warn",
            backend="docker",
            docker_pids_limit=256,
            docker_memory="1g",
            docker_cpus="1.5",
            docker_read_only=True,
            protect_repo_meta=True,
            docker_env_allowlist=["LANG", "GIT_AUTHOR_NAME"],
        ),
        tmp_path,
    )

    assert isinstance(runner, DockerBackgroundRunner)
    assert runner.pids_limit == 256
    assert runner.memory_limit == "1g"
    assert runner.cpus == "1.5"
    assert runner.read_only_rootfs is True
    assert runner.protect_repo_meta is True
    assert runner.env_allowlist == ("LANG", "GIT_AUTHOR_NAME")


def test_build_bwrap_argv_matches_inline_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PATH", os.defpath)
    monkeypatch.setattr(
        sandbox_runner_mod.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("_build_bwrap_argv must not call subprocess.run"),
    )
    monkeypatch.setattr(sandbox_runner_mod.shutil, "which", lambda *_args, **_kwargs: None)

    first_argv, first_env = sandbox_runner_mod._build_bwrap_argv(
        root=tmp_path,
        cwd=tmp_path,
        cmd="echo hi",
        network="off",
        clear_env=False,
        profile="hardened",
        unshare_cgroup=False,
    )
    second_argv, second_env = sandbox_runner_mod._build_bwrap_argv(
        root=tmp_path,
        cwd=tmp_path,
        cmd="echo hi",
        network="off",
        clear_env=False,
        profile="hardened",
        unshare_cgroup=False,
    )

    assert second_argv == first_argv
    assert second_env == first_env
    assert first_argv[-3:] == ["sh", "-lc", "echo hi"]
