from __future__ import annotations

import subprocess

from alysis_code.agent_runtimes.builtins import (
    RUNTIME_PLUGIN_OPT_IN_ENV,
    create_builtin_runtime_registry,
    runtime_setup_options,
)
from alysis_code.agent_runtimes.codex_cli import CodexCliRuntimeAdapter
from alysis_code.config import AgentRuntimeSettings


def _settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(adapter="codex-cli", executable="codex")


def test_builtin_codex_runtime_metadata_and_registry() -> None:
    options = runtime_setup_options()

    assert [option.id for option in options] == ["openai-codex"]
    assert options[0].adapter == "codex-cli"
    assert options[0].default_executable == "codex"
    assert create_builtin_runtime_registry().runtime_ids() == ("openai-codex",)


def test_runtime_entry_points_are_not_loaded_without_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.delenv(RUNTIME_PLUGIN_OPT_IN_ENV, raising=False)
    monkeypatch.setattr(
        "alysis_code.agent_runtimes.builtins.entry_points",
        lambda: (_ for _ in ()).throw(AssertionError("entry points must not load")),
    )

    assert [option.id for option in runtime_setup_options()] == ["openai-codex"]


def test_legacy_runtime_plugin_opt_in_is_honored(monkeypatch) -> None:
    calls: list[str] = []

    class _EntryPoints:
        def select(self, *, group: str):
            calls.append(group)
            return ()

    monkeypatch.delenv(RUNTIME_PLUGIN_OPT_IN_ENV, raising=False)
    monkeypatch.setenv("ALYSIS_ENABLE_AGENT_RUNTIME_PLUGINS", "1")
    monkeypatch.setattr(
        "alysis_code.agent_runtimes.builtins.entry_points",
        lambda: _EntryPoints(),
    )

    assert [option.id for option in runtime_setup_options()] == ["openai-codex"]
    assert calls == ["alysis.agent_runtimes"]


def test_codex_probe_reports_missing_executable(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)

    result = CodexCliRuntimeAdapter().probe(_settings())

    assert result.available is False
    assert result.executable == "codex"
    assert "not installed" in str(result.detail)


def test_codex_account_status_is_opaque_and_detects_chatgpt(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/codex")
    commands: list[list[str]] = []
    environments: list[dict[str, str]] = []
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        commands.append(list(command))
        environments.append(dict(kwargs.get("env") or {}))
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "codex-cli 1.2.3\n", "")
        return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status = CodexCliRuntimeAdapter().account_status(_settings())

    assert status.authenticated is True
    assert status.auth_method_id == "chatgpt"
    assert status.account_label is None
    assert commands == [
        ["/usr/local/bin/codex", "--version"],
        ["/usr/local/bin/codex", "login", "status"],
    ]
    assert environments and all("OPENAI_API_KEY" not in env for env in environments)


def test_codex_account_status_rejects_api_key_login_for_account_runtime(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/codex")

    def fake_run(command, **_kwargs):  # type: ignore[no-untyped-def]
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "codex-cli 1.2.3\n", "")
        return subprocess.CompletedProcess(command, 0, "Logged in using an API key\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status = CodexCliRuntimeAdapter().account_status(_settings())

    assert status.verified is True
    assert status.authenticated is False
    assert status.auth_method_id == "api-key"
    assert "not with a ChatGPT account" in str(status.detail)


def test_codex_device_login_uses_official_command_and_rechecks_status(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/codex")
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):  # type: ignore[no-untyped-def]
        commands.append(list(command))
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "codex-cli 1.2.3\n", "")
        if command[-2:] == ["login", "status"]:
            return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT\n", "")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    status = CodexCliRuntimeAdapter().login(_settings(), "device-code")

    assert status.authenticated is True
    assert ["/usr/local/bin/codex", "login", "--device-auth"] in commands
    assert commands[-1] == ["/usr/local/bin/codex", "login", "status"]


def test_codex_login_rejects_unknown_auth_method_without_starting_login(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/codex")
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):  # type: ignore[no-untyped-def]
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, "codex-cli 1.2.3\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    status = CodexCliRuntimeAdapter().login(_settings(), "copied-browser-token")

    assert status.authenticated is False
    assert "Unsupported" in str(status.detail)
    assert commands == [["/usr/local/bin/codex", "--version"]]


def test_codex_logout_uses_provider_owned_credentials(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/codex")
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):  # type: ignore[no-untyped-def]
        commands.append(list(command))
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "codex-cli 1.2.3\n", "")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    status = CodexCliRuntimeAdapter().logout(_settings())

    assert status.authenticated is False
    assert status.detail == "Codex is signed out."
    assert commands[-1] == ["/usr/local/bin/codex", "logout"]
