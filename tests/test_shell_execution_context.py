"""The model must use the shell selected for this session, including after switches."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from alysis_code import sandbox_runner
from alysis_code.agent import prompt_context
from alysis_code.agent_loop import create_session
from alysis_code.cli_impl.chat import loop as chat_loop
from alysis_code.cli_impl.commands import welcome
from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.sandbox_settings import (
    apply_sandbox_mode_to_config,
    resolve_shell_sandbox_settings,
)
from alysis_code.windows_mxc import WindowsMxcRunner


@pytest.fixture(autouse=True)
def controlled_shell_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("ALYSIS_SHELL_SANDBOX_") or key.upper() == "COMSPEC":
            monkeypatch.delenv(key)
    monkeypatch.setattr(sandbox_runner.shutil, "which", lambda _name: None)
    monkeypatch.setattr(prompt_context.platform, "system", lambda: "Windows")


def _config(**sandbox: object) -> AppConfig:
    cfg = AppConfig(
        model="test-model", skills_enabled=False, subagents_enabled=False, web_search_mode="off"
    )
    cfg.extra_fields["shell_sandbox"] = sandbox
    return cfg


@pytest.mark.parametrize("backend", ["windows-native", "windows-mxc"])
@pytest.mark.parametrize("mode", ["auto", "review"])
def test_windows_sandbox_discloses_windows_powershell_without_starting_helper(
    backend: str, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_runner_build(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Prompt assembly must not resolve/start a native helper")

    monkeypatch.setattr(sandbox_runner, "build_shell_runner_from_settings", no_runner_build)
    line = prompt_context._shell_execution_context_line(_config(backend=backend), mode=mode)
    assert f"backend={backend}" in line
    assert "shell=Windows PowerShell (powershell.exe, not pwsh)" in line
    assert "successful helper validation" in line


@pytest.mark.parametrize("comspec", [None, r"C:\Windows\System32\cmd.exe"])
def test_windows_host_discloses_cmd_not_the_users_interactive_shell(
    comspec: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHELL", "pwsh.exe")
    if comspec:
        monkeypatch.setenv("COMSPEC", comspec)
    line = prompt_context._shell_execution_context_line(
        _config(mode="off", backend="windows-mxc"), mode="auto"
    )
    assert "backend=host; shell=cmd.exe; use cmd.exe syntax" in line
    assert "backend=windows-mxc" not in line


def test_custom_windows_host_shell_does_not_get_a_false_syntax_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMSPEC", r"C:\Tools\custom-shell.exe")
    line = prompt_context._shell_execution_context_line(_config(mode="off"), mode="auto")
    assert "custom COMSPEC" in line
    assert "syntax is not inferred" in line
    assert "use cmd.exe syntax" not in line


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_posix_host_including_wsl_ignores_inherited_windows_shell(
    system: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prompt_context.platform, "system", lambda: system)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    line = prompt_context._shell_execution_context_line(_config(mode="off"), mode="auto")
    assert "backend=host; shell=/bin/sh; use POSIX shell syntax" in line
    assert "PowerShell" not in line
    assert "cmd.exe" not in line


@pytest.mark.parametrize(
    ("system", "backend"), [("Linux", "bwrap"), ("Windows", "docker"), ("Darwin", "seatbelt")]
)
def test_non_windows_sandbox_shells_are_posix_even_on_windows_host(
    system: str, backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prompt_context.platform, "system", lambda: system)
    monkeypatch.setattr(sandbox_runner.shutil, "which", lambda _name: "/bin/test-backend")
    monkeypatch.setattr(sandbox_runner.os.path, "exists", lambda _path: True)
    line = prompt_context._shell_execution_context_line(_config(backend=backend), mode="auto")
    assert f"backend={backend}; shell=sh; use POSIX shell syntax" in line


def test_unavailable_backend_does_not_claim_host_execution_or_powershell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Windows auto selects the bundled native backend without probing external
    # executables; use the explicitly missing Docker backend for this case.
    cfg = _config(backend="docker", unavailable_fallback="refuse")
    assert "unavailable; no selected sandbox backend and no host fallback" in (
        prompt_context._shell_execution_context_line(cfg, mode="auto")
    )
    cfg.extra_fields["shell_sandbox"]["unavailable_fallback"] = "ask"
    line = prompt_context._shell_execution_context_line(cfg, mode="auto")
    assert "backend=host; shell=cmd.exe" in line
    assert "Each command requires approval" in line
    monkeypatch.setattr(prompt_context.platform, "system", lambda: "Linux")
    line = prompt_context._shell_execution_context_line(
        _config(backend="windows-mxc", unavailable_fallback="ask"), mode="auto"
    )
    assert "unavailable on this platform" in line
    assert "PowerShell" not in line
    assert "backend=host" not in line


@pytest.mark.parametrize(("mode", "sensitive"), [("readonly", False), ("auto", True)])
def test_disabled_session_has_no_active_shell(mode: str, sensitive: bool) -> None:
    line = prompt_context._shell_execution_context_line(
        _config(backend="windows-mxc"), mode=mode, sensitive_media=sensitive
    )
    assert line == "shell_run: disabled in this session."


def test_invalid_configuration_does_not_promise_a_host_fallback() -> None:
    line = prompt_context._shell_execution_context_line(_config(backend="custom"), mode="auto")
    assert "unresolved" in line
    assert "backend=host" not in line


def test_initial_prompt_uses_resolved_session_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A persisted sandbox mode must not override the effective process setting.
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    prepared = prompt_context.prepare_session_prompt_context(
        cfg=_config(mode="strict", backend="windows-mxc"),
        root=tmp_path,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=False,
    )
    context = next(
        message["content"]
        for message in prepared.messages
        if str(message.get("content", "")).startswith("<environment_context>")
    )
    assert "backend=host; shell=cmd.exe" in context
    assert "backend=windows-mxc" not in context


def test_created_session_carries_effective_settings_across_config_edits(tmp_path: Path) -> None:
    session = create_session(
        cfg=_config(mode="off", backend="windows-mxc"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="test-key",
        verification_enabled=False,
    )
    try:
        assert isinstance(session.shell_runner, sandbox_runner.LazyShellRunner)
        assert session.shell_runner.execution_settings.mode == "off"
        apply_sandbox_mode_to_config(session.cfg, "strict")
        assert prompt_context.refresh_session_environment_context_message(session)
        context = next(
            message["content"]
            for message in session.messages
            if str(message.get("content", "")).startswith("<environment_context>")
        )
        assert "backend=host; shell=cmd.exe" in context
        assert "backend=windows-mxc" not in context
    finally:
        session.close()


def test_bridge_permission_switch_refreshes_shell_from_current_resolved_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config(mode="strict", backend="windows-mxc")
    captured_settings = resolve_shell_sandbox_settings(cfg)
    native = WindowsMxcRunner(captured_settings)
    lazy = sandbox_runner.LazyShellRunner(lambda: native, settings=captured_settings)
    session = SimpleNamespace(
        cfg=cfg,
        mode="review",
        surface=None,
        store=None,
        shell_runner=lazy,
        messages=[{"role": "user", "content": "<environment_context>\n</environment_context>\n"}],
    )
    assert prompt_context.refresh_session_environment_context_message(session)
    assert "backend=windows-mxc; shell=Windows PowerShell" in session.messages[0]["content"]
    # Use the real bridge/CLI mutation path and refresh hook; only tool creation
    # is stubbed so this contract never probes or executes an installed helper.
    for name in ("_rebuild_session_tools_for_mode", "refresh_session_environment_context_message"):
        monkeypatch.delitem(chat_loop.__dict__, name, raising=False)
    monkeypatch.setattr(welcome, "_rebuild_session_tools_for_mode", lambda **_kwargs: None)

    stdio_bridge._apply_agent_session_mode(session, "auto")
    assert "mode: auto" in session.messages[0]["content"]
    assert "backend=windows-mxc" in session.messages[0]["content"]

    apply_sandbox_mode_to_config(session.cfg, "off")
    stdio_bridge._apply_agent_session_mode(session, "auto")
    # Tools retain their runner during config refresh. A changed preference
    # must not announce a backend replacement that did not actually happen.
    assert "backend=windows-mxc" in session.messages[0]["content"]
    assert "backend=host" not in session.messages[0]["content"]
    assert lazy.resolved_runner is None

    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    assert lazy._resolve_runner() is native  # Construct only; no native execution.
    stdio_bridge._apply_agent_session_mode(session, "review")
    assert "backend=windows-mxc; shell=Windows PowerShell" in session.messages[0]["content"]
    assert "backend=host" not in session.messages[0]["content"]

    # A replaced runner (as on session recreation) is authoritative even if
    # config/env have since changed again.
    session.shell_runner = sandbox_runner.HostShellRunner()
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "strict")
    stdio_bridge._apply_agent_session_mode(session, "auto")
    assert "backend=host; shell=cmd.exe" in session.messages[0]["content"]
    assert "backend=windows-mxc" not in session.messages[0]["content"]

    stdio_bridge._apply_agent_session_mode(session, "readonly")
    assert "shell_run: disabled" in session.messages[0]["content"]


def test_loaded_auto_runner_is_not_reselected_when_availability_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config(backend="auto", unavailable_fallback="ask")
    host = sandbox_runner.HostShellRunner(requires_command_approval=True)
    lazy = sandbox_runner.LazyShellRunner(
        lambda: host, settings=resolve_shell_sandbox_settings(cfg)
    )
    assert lazy._resolve_runner() is host
    monkeypatch.setattr(sandbox_runner.shutil, "which", lambda _name: "docker.exe")
    line = prompt_context._shell_execution_context_line(cfg, mode="auto", runner=lazy)
    assert "backend=host; shell=cmd.exe" in line
    assert "Each command requires approval" in line
    assert "backend=docker" not in line


def test_retained_disabled_runner_does_not_claim_execution_after_mode_change() -> None:
    line = prompt_context._shell_execution_context_line(
        _config(mode="off"), mode="auto", runner=sandbox_runner.DisabledShellRunner()
    )
    assert line == "shell_run: disabled in this session."


@pytest.mark.parametrize("lazy", [False, True])
def test_custom_runner_does_not_inherit_a_shell_hint_from_config(lazy: bool) -> None:
    def must_not_load() -> None:
        pytest.fail("Disclosure must not initialize an unknown runner")

    runner = sandbox_runner.LazyShellRunner(must_not_load) if lazy else object()
    line = prompt_context._shell_execution_context_line(
        _config(backend="windows-mxc"), mode="auto", runner=runner
    )
    assert "custom runner; command syntax is not inferred" in line
