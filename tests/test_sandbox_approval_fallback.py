"""Shell sandbox selection, the per-command approval fallback, and the macOS Seatbelt backend."""

from __future__ import annotations

import io
import os
import shlex
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from rich.console import Console

import alysis_code.sandbox_runner as sandbox_runner_mod
from alysis_code.agent_loop import AgentRuntimeError, build_tools
from alysis_code.background_runner import (
    DisabledBackgroundRunner,
    HostBackgroundRunner,
    SeatbeltBackgroundRunner,
    build_background_shell_runner_from_settings,
)
from alysis_code.config import AppConfig, ConfigError
from alysis_code.sandbox_runner import (
    DisabledShellRunner,
    HostShellRunner,
    LazyShellRunner,
    SandboxBackendChoice,
    SeatbeltShellRunner,
    build_shell_runner_from_settings,
    seatbelt_command,
    seatbelt_profile,
    select_shell_sandbox_backend,
)
from alysis_code.sandbox_settings import ShellSandboxSettings, resolve_shell_sandbox_settings
from alysis_code.session_store import SessionStore
from alysis_code.surface import ApprovalDecision, ApprovalRequest
from alysis_code.surface.noop_surface import NoopSurface

STRICT = ShellSandboxSettings(mode="strict")
ASK = replace(STRICT, unavailable_fallback="ask")


def _choose(settings, system, tools=(), seatbelt=False):  # type: ignore[no-untyped-def]
    return select_shell_sandbox_backend(
        settings,
        system=system,
        which=lambda name: f"/usr/bin/{name}" if name in tools else None,
        path_exists=lambda path: seatbelt and path == sandbox_runner_mod.SEATBELT_EXECUTABLE,
    )


@pytest.mark.parametrize(
    ("settings", "system", "tools", "seatbelt", "expected"),
    [
        (STRICT, "Linux", ("bwrap", "docker"), False, SandboxBackendChoice("bwrap")),
        (STRICT, "Linux", ("docker",), False, SandboxBackendChoice("docker")),
        # macOS keeps Docker when it is installed; Seatbelt fills the gap when it is not.
        (STRICT, "Darwin", ("docker",), True, SandboxBackendChoice("docker")),
        (STRICT, "Darwin", (), True, SandboxBackendChoice("seatbelt")),
        (
            STRICT,
            "Windows",
            (),
            False,
            SandboxBackendChoice(None, "auto backend could not find bwrap or docker"),
        ),
        (
            ASK,
            "Windows",
            (),
            False,
            SandboxBackendChoice(
                "host",
                "auto backend could not find bwrap or docker",
                requires_command_approval=True,
            ),
        ),
        (ASK, "Windows", ("docker",), False, SandboxBackendChoice("docker")),
        (replace(ASK, mode="off"), "Windows", (), False, SandboxBackendChoice("host")),
        (
            replace(STRICT, backend="seatbelt"),
            "Linux",
            (),
            False,
            SandboxBackendChoice(
                None, "seatbelt backend selected, but sandbox-exec is not available (macOS only)"
            ),
        ),
    ],
)
def test_backend_selection(settings, system, tools, seatbelt, expected) -> None:  # type: ignore[no-untyped-def]
    assert _choose(settings, system, tools, seatbelt) == expected


def test_the_fallback_is_read_from_config_and_environment(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("ALYSIS_SHELL_SANDBOX_FALLBACK", raising=False)
    assert resolve_shell_sandbox_settings(AppConfig()).unavailable_fallback == "refuse"
    cfg = AppConfig()
    cfg.extra_fields["shell_sandbox"] = {"unavailable_fallback": "ask"}
    assert resolve_shell_sandbox_settings(cfg).unavailable_fallback == "ask"
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_FALLBACK", "refuse")
    assert resolve_shell_sandbox_settings(cfg).unavailable_fallback == "refuse"
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_FALLBACK", "maybe")
    with pytest.raises(ConfigError):
        resolve_shell_sandbox_settings(AppConfig())


@pytest.fixture
def no_sandbox_here(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(sandbox_runner_mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sandbox_runner_mod.shutil, "which", lambda _name: None)


def test_builders_follow_the_fallback(no_sandbox_here, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    runner = build_shell_runner_from_settings(ASK, tmp_path)
    assert runner == HostShellRunner(requires_command_approval=True)
    assert isinstance(
        build_background_shell_runner_from_settings(ASK, tmp_path), HostBackgroundRunner
    )
    with pytest.raises(ConfigError, match="ALYSIS_SHELL_SANDBOX_FALLBACK=ask"):
        build_shell_runner_from_settings(STRICT, tmp_path)
    assert isinstance(
        build_shell_runner_from_settings(replace(STRICT, mode="warn"), tmp_path),
        DisabledShellRunner,
    )
    assert isinstance(
        build_background_shell_runner_from_settings(replace(STRICT, mode="warn"), tmp_path),
        DisabledBackgroundRunner,
    )
    lazy = LazyShellRunner(lambda: build_shell_runner_from_settings(ASK, tmp_path))
    assert lazy.requires_command_approval is True
    assert (
        LazyShellRunner(
            lambda: build_shell_runner_from_settings(STRICT, tmp_path)
        ).requires_command_approval
        is False
    )


def test_builders_choose_seatbelt_on_macos_without_docker(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(sandbox_runner_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sandbox_runner_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(sandbox_runner_mod.os.path, "exists", lambda path: True)
    assert isinstance(build_shell_runner_from_settings(STRICT, tmp_path), SeatbeltShellRunner)
    assert isinstance(
        build_background_shell_runner_from_settings(STRICT, tmp_path), SeatbeltBackgroundRunner
    )


class _ApprovalSurface(NoopSurface):
    host_managed_approvals = True

    def __init__(self, allow: bool) -> None:
        self.allow = allow
        self.requests: list[ApprovalRequest] = []

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(allow=self.allow)


def _tools(tmp_path: Path, surface, *, mode: str = "auto", cfg=None):  # type: ignore[no-untyped-def]
    return build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        surface=surface,
        store=SessionStore(
            enabled=False,
            sessions_dir=tmp_path / "sessions",
            session_id="sandbox-fallback",
            cwd=str(tmp_path),
            repo_root=str(tmp_path),
        ),
        mode=mode,
        yes=True,  # auto-confirm does not skip the approval that replaces a sandbox
        non_interactive=True,
        cfg=cfg,
        shell_runner=HostShellRunner(requires_command_approval=True),
    )


def _python_command(code: str) -> str:
    executable = f'"{sys.executable}"' if sys.platform == "win32" else shlex.quote(sys.executable)
    return f'{executable} -c "{code}"'


def test_each_command_runs_only_after_approval(tmp_path: Path) -> None:
    surface = _ApprovalSurface(allow=True)
    command = _python_command("print('approved')")
    result = _tools(tmp_path, surface)["shell_run"].run({"cmd": command})
    assert "approved" in str(result.get("stdout"))
    assert [r.command for r in surface.requests] == [command]
    assert "No sandbox is available on this computer" in surface.requests[0].reason


def test_read_only_commands_need_no_approval_even_without_a_sandbox(tmp_path: Path) -> None:
    surface = _ApprovalSurface(allow=False)
    result = _tools(tmp_path, surface)["shell_run"].run({"cmd": "echo read-only"})
    assert "read-only" in str(result.get("stdout"))
    assert surface.requests == []


def test_a_declined_command_does_not_run(tmp_path: Path) -> None:
    surface = _ApprovalSurface(allow=False)
    marker = tmp_path / "ran.txt"
    command = f"{shlex.quote(sys.executable)} -c \"open('ran.txt', 'w').close()\""
    with pytest.raises(AgentRuntimeError, match="User declined: shell_run"):
        _tools(tmp_path, surface)["shell_run"].run({"cmd": command})
    assert not marker.exists()


def test_full_access_never_asks(tmp_path: Path) -> None:
    surface = _ApprovalSurface(allow=False)
    _tools(tmp_path, surface, mode="fullaccess")["shell_run"].run(
        {"cmd": _python_command("print('ok')")}
    )
    assert surface.requests == []


def test_verification_asks_once_for_its_commands(
    no_sandbox_here, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_FALLBACK", "ask")
    monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "strict")
    (tmp_path / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    surface = _ApprovalSurface(allow=False)
    tools = _tools(tmp_path, surface, cfg=AppConfig())
    with pytest.raises(AgentRuntimeError, match="User declined: verify_run"):
        tools["verify_run"].run({"commands": ["python -m pytest -q"]})
    assert [r.kind for r in surface.requests] == ["verify_run"]


def test_seatbelt_profile_limits_writes_and_network() -> None:
    closed = seatbelt_profile(network="off", protected_path_count=2)
    assert closed.splitlines()[:3] == ["(version 1)", "(allow default)", "(deny file-write*)"]
    assert '(subpath (param "WORKSPACE"))' in closed
    assert '(deny file-write* (subpath (param "PROTECTED_1")))' in closed
    assert '(deny network-outbound (remote ip "*:*"))' in closed
    assert "network" not in seatbelt_profile(network="on", protected_path_count=0)


def test_seatbelt_command_protects_repository_metadata_except_for_plain_commits(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    edit = shlex.split(
        seatbelt_command(
            "rm -rf .git",
            root=tmp_path,
            network="off",
            protect_repo_meta=True,
            safe_git_writes=True,
        )
    )
    commit = shlex.split(
        seatbelt_command(
            "git commit -m x",
            root=tmp_path,
            network="off",
            protect_repo_meta=True,
            safe_git_writes=True,
        )
    )
    assert edit[0] == "/usr/bin/sandbox-exec"
    assert f"WORKSPACE={os.path.realpath(tmp_path)}" in edit
    assert f"PROTECTED_0={os.path.realpath(tmp_path / '.git')}" in edit
    assert edit[-3:] == ["/bin/sh", "-c", "rm -rf .git"]
    assert not any(part.startswith("PROTECTED_") for part in commit)


macos_only = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"),
    reason="runs the real macOS sandbox",
)


def _run_in_seatbelt(
    tmp_path: Path, script: str, *, network: str = "off"
) -> subprocess.CompletedProcess[str]:
    return SeatbeltShellRunner(network=network).run(
        root=tmp_path,
        cwd=tmp_path,
        cmd=f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
        timeout_s=30,
    )


@macos_only
def test_macos_seatbelt_allows_workspace_writes_and_blocks_others(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = Path.home() / f".alysis-seatbelt-probe-{os.getpid()}"
    inside = _run_in_seatbelt(workspace, "open('inside.txt','w').write('ok')")
    assert inside.returncode == 0, inside.stderr
    assert (workspace / "inside.txt").read_text() == "ok"
    blocked = _run_in_seatbelt(workspace, f"open({str(outside)!r},'w').write('no')")
    assert blocked.returncode != 0
    assert not outside.exists()


@macos_only
def test_macos_seatbelt_without_network_still_reaches_this_machine(tmp_path: Path) -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        local = _run_in_seatbelt(
            tmp_path, f"import socket; socket.create_connection(('127.0.0.1', {port}), 5).close()"
        )
        assert local.returncode == 0, local.stderr
        remote = _run_in_seatbelt(
            tmp_path, "import socket; socket.create_connection(('1.1.1.1', 80), 5).close()"
        )
        assert remote.returncode != 0
    finally:
        server.close()
