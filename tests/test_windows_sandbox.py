from __future__ import annotations

import hashlib
import json
import os
import subprocess
import traceback
from dataclasses import replace
from pathlib import Path

import pytest

from alysis_code import windows_sandbox as native
from alysis_code.background_runner import build_background_shell_runner_from_settings
from alysis_code.config import AppConfig, ConfigError
from alysis_code.sandbox_doctor import diagnose_sandbox
from alysis_code.sandbox_runner import build_shell_runner_from_settings
from alysis_code.sandbox_settings import ShellSandboxSettings, resolve_shell_sandbox_settings


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(native, "_powershell_execution_policy", lambda _shell: "RemoteSigned")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    root = tmp_path / "work"
    root.mkdir()
    return root


def request(root: Path, settings: ShellSandboxSettings | None = None, **kwargs):
    helper = kwargs.pop("helper", root.parent / "helper" / "alysis-sandbox.exe")
    helper.parent.mkdir(exist_ok=True)
    return native.build_request(
        settings or ShellSandboxSettings(backend="windows-native"),
        root=root,
        cwd=kwargs.pop("cwd", root),
        cmd="Write-Output 'hello; & world'",
        helper=helper,
        **kwargs,
    )


def test_read_only_never_grants_workspace_writes(workspace):
    result = request(workspace, ShellSandboxSettings(filesystem_access="read-only"))
    assert result["write"] == []
    assert str(workspace) in result["deny_write"]
    assert str(workspace) in result["read"]


def test_workspace_write_keeps_metadata_protected(workspace):
    result = request(workspace)
    assert result["write"] == [str(workspace)]
    for name in (
        ".git",
        ".alysis",
        ".codex",
        ".agents",
        ".alysis_skills",
        ".github/skills",
        "AGENTS.md",
        "CLAUDE.md",
        "CONVENTIONS.md",
    ):
        assert any(Path(path) == workspace / name for path in result["deny_write"])
    assert result["argv"][-1].endswith("; Write-Output 'hello; & world'")
    assert "-NoProfile" in result["argv"]


def test_all_discovered_project_skill_roots_are_reserved(workspace):
    from alysis_code.skills.discovery import project_skill_root_relative_paths

    denied = [Path(path) for path in request(workspace)["deny_write"]]
    for parts in project_skill_root_relative_paths():
        assert any(workspace.joinpath(*parts).is_relative_to(path) for path in denied)
    assert workspace / ".github" not in denied
    assert not (workspace / ".github").exists(), "Policy construction must not create placeholders"


@pytest.mark.parametrize(
    "change",
    [
        {"network": "invalid"},
        {"clear_env": False},
        {"protect_repo_meta": False},
        {"filesystem_access": "all"},
    ],
)
def test_unsupported_policy_never_reaches_spawn(workspace, change):
    with pytest.raises(ConfigError):
        request(workspace, replace(ShellSandboxSettings(), **change))


def test_cwd_escape_is_rejected(workspace):
    with pytest.raises(ConfigError, match="inside"):
        request(workspace, cwd=workspace.parent)


@pytest.mark.parametrize("network", ["off", "on"])
@pytest.mark.parametrize("access", ["read-only", "workspace-write"])
def test_network_policy_does_not_broaden_filesystem_permissions(workspace, network, access):
    policy = request(workspace, ShellSandboxSettings(network=network, filesystem_access=access))
    assert policy["protocol"] == 3
    assert policy["network"] == network
    assert policy["write"] == ([str(workspace)] if access == "workspace-write" else [])
    assert str(workspace / "AGENTS.md") in policy["deny_write"]
    assert str(workspace / "CLAUDE.md") in policy["deny_write"]
    assert str(workspace / "CONVENTIONS.md") in policy["deny_write"]


@pytest.mark.parametrize("access", ["read-only", "workspace-write"])
def test_existing_hardlink_is_rejected_before_grants(workspace, access):
    private = workspace.parent / "private.txt"
    private.write_text("canary")
    (workspace / "alias.txt").hardlink_to(private)
    with pytest.raises(ConfigError, match="hardlinked files"):
        request(workspace, ShellSandboxSettings(filesystem_access=access))
    assert private.read_text() == "canary"


@pytest.mark.parametrize("source", ["tools", "helper"])
def test_every_recursive_read_root_rejects_existing_hardlinks(workspace, source):
    directory = workspace.parent / source
    nested = directory / "nested"
    nested.mkdir(parents=True)
    private = workspace.parent / "private.txt"
    private.write_text("private canary")
    (nested / "alias.txt").hardlink_to(private)
    settings = ShellSandboxSettings(
        windows_read_roots=(str(directory),) if source == "tools" else ()
    )
    with pytest.raises(ConfigError, match="hardlinked files"):
        request(workspace, settings, helper=workspace.parent / "helper" / "alysis-sandbox.exe")
    assert private.read_text() == "private canary"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
@pytest.mark.parametrize("source", ["tools", "helper"])
def test_every_recursive_read_root_rejects_existing_junctions(workspace, source):
    directory = workspace.parent / source
    directory.mkdir()
    private = workspace.parent / "private"
    private.mkdir()
    junction = directory / "alias"
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(private)],
        check=True,
        capture_output=True,
    )
    try:
        with pytest.raises(ConfigError, match="reparse points"):
            request(
                workspace,
                ShellSandboxSettings(
                    windows_read_roots=(str(directory),) if source == "tools" else ()
                ),
            )
    finally:
        junction.rmdir()


def test_recursive_read_roots_are_validated_once(workspace, monkeypatch):
    from alysis_code import windows_toolchain

    monkeypatch.setattr(windows_toolchain, "installed_tools", lambda: {})
    checked = []
    monkeypatch.setattr(native, "_validate_workspace_tree", checked.append)
    result = request(
        workspace,
        ShellSandboxSettings(windows_read_roots=(str(workspace),), filesystem_access="read-only"),
        helper=workspace / "helper.exe",
    )
    assert checked == [workspace]
    assert result["read"] == [str(workspace)]


def test_tool_read_root_cannot_become_a_write_root(workspace):
    tools = workspace.parent / "tools"
    tools.mkdir()
    result = request(workspace, ShellSandboxSettings(windows_read_roots=(str(tools),)))
    assert str(tools) in result["read"]
    assert str(tools) in result["deny_write"]
    assert str(tools) not in result["write"]
    with pytest.raises(ConfigError, match="contain the workspace"):
        request(workspace, ShellSandboxSettings(windows_read_roots=(str(workspace.parent),)))


def test_host_virtual_environment_inside_workspace_is_protected(workspace, monkeypatch):
    runtime = workspace / ".venv"
    runtime.mkdir()
    monkeypatch.setattr(native.sys, "prefix", str(runtime))
    assert str(runtime) in request(workspace)["deny_write"]


def test_ancestor_traversal_never_grants_sibling_reads(workspace):
    workspace = Path.home() / "project"
    workspace.mkdir()
    result = request(workspace)
    assert str(workspace.parent) in result["traverse"]
    assert str(workspace.parent) not in result["read"]
    assert str(workspace.parent) not in result["write"]
    assert str(workspace.parent.parent) not in result["traverse"]


def test_secrets_and_startup_injection_are_not_forwarded(workspace, monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "ALYSIS_API_KEY",
        "PYTHONPATH",
        "NODE_OPTIONS",
        "AWS_SECRET_ACCESS_KEY",
        "HTTPS_PROXY",
    ):
        monkeypatch.setenv(name, "never-forward-this")
    assert "never-forward-this" not in json.dumps(request(workspace)["env"])
    assert dict(request(workspace)["env"])["NODE_OPTIONS"] == (
        "--preserve-symlinks --preserve-symlinks-main"
    )
    with pytest.raises(ConfigError, match="not allowed"):
        request(workspace, env_overrides={"PYTHONPATH": str(workspace)})
    with pytest.raises(ConfigError, match="not allowed"):
        request(workspace, env_overrides={"NODE_OPTIONS": "--require attacker.js"})
    for name in ("PSExecutionPolicyPreference", "PSModulePath"):
        with pytest.raises(ConfigError, match="not allowed"):
            request(workspace, env_overrides={name: "Bypass"})


@pytest.mark.parametrize("policy", sorted(native._EXECUTION_POLICIES))
def test_host_policy_query_is_fixed_isolated_and_bounded(tmp_path, monkeypatch, policy):
    shell = tmp_path / "Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    calls = []
    monkeypatch.setenv("PSModulePath", "untrusted-user-module-path")
    monkeypatch.delenv("PSExecutionPolicyPreference", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "private-key-not-for-probe")

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, (policy + "\r\n").encode(), b"")

    monkeypatch.setattr(native.subprocess, "run", run)
    assert native._powershell_execution_policy(shell) == policy
    argv, kwargs = calls[0]
    assert argv == [
        str(shell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "$ErrorActionPreference='Stop'; [Console]::Out.Write(("
        r"Microsoft.PowerShell.Security\Get-ExecutionPolicy).ToString())",
    ]
    assert kwargs["cwd"] == shell.parent
    assert kwargs["env"]["PSModulePath"] == str(shell.parent / "Modules")
    assert kwargs["env"]["SYSTEMROOT"] == str(tmp_path / "Windows")
    assert set(kwargs["env"]) == {"SYSTEMROOT", "WINDIR", "PATH", "PSModulePath"}
    assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["timeout"] == 10


@pytest.mark.parametrize("policy", ["AllSigned", "Restricted", "RemoteSigned", "Undefined"])
def test_host_process_policy_is_preserved_before_persisted_preferences(
    tmp_path, monkeypatch, policy
):
    monkeypatch.setenv("PSExecutionPolicyPreference", policy)

    def query(argv, **kwargs):
        assert kwargs["env"]["PSExecutionPolicyPreference"] == policy
        return subprocess.CompletedProcess(argv, 0, b"Restricted", b"")

    monkeypatch.setattr(native.subprocess, "run", query)
    assert (
        native._powershell_execution_policy(
            tmp_path / "Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        == "Restricted"
    )


@pytest.mark.parametrize("policy", ["", "Default", "RemoteSigned; exit", "AllSigned\nBypass"])
def test_invalid_host_process_policy_never_starts_query(tmp_path, monkeypatch, policy):
    monkeypatch.setenv("PSExecutionPolicyPreference", policy)
    monkeypatch.setattr(
        native.subprocess,
        "run",
        lambda *a, **k: pytest.fail("malformed policy reached host process"),
    )
    with pytest.raises(ConfigError, match="Invalid host.*process policy"):
        native._powershell_execution_policy(
            tmp_path / "Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        )


@pytest.mark.parametrize(
    "stdout,stderr,code,reason",
    [
        (b"", b"", 0, "invalid response"),
        (b"Undefined", b"", 0, "invalid response"),
        (b"Default", b"", 0, "invalid response"),
        (b"RemoteSigned\nBypass", b"", 0, "invalid response"),
        (b"remotesigned", b"", 0, "invalid response"),
        (b"RemoteSigned\x00", b"", 0, "invalid response"),
        (b"private-query-output-\xff", b"", 0, "invalid response"),
        (b"RemoteSigned", b"private-query-output", 0, "produced stderr"),
        (b"private-query-output", b"", 1, "returncode=1"),
        (b"private-query-output", b"private-query-output", -1, "returncode=-1"),
    ],
)
def test_host_policy_query_rejects_ambiguous_results(
    tmp_path, monkeypatch, stdout, stderr, code, reason
):
    monkeypatch.setattr(
        native.subprocess,
        "run",
        lambda argv, **_: subprocess.CompletedProcess(argv, code, stdout, stderr),
    )
    with pytest.raises(ConfigError, match="no sandbox command was launched") as caught:
        native._powershell_execution_policy(
            tmp_path / "Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        )
    assert reason in str(caught.value)
    assert "private-query-output" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "error,winerror,reason",
    [
        (OSError("private-query-output"), None, "OS error code unavailable"),
        (OSError(2, "private-query-output"), None, "errno=2"),
        (OSError("private-query-output"), 5, "winerror=5"),
        (ValueError("private-query-output"), None, "invalid arguments"),
        (
            subprocess.TimeoutExpired(
                "private-query-output",
                10,
                output=b"private-query-output",
                stderr=b"private-query-output",
            ),
            None,
            "timed out after 10 seconds",
        ),
        (subprocess.SubprocessError("private-query-output"), None, "query process failed"),
    ],
)
def test_host_policy_query_failure_has_no_policy_fallback(
    tmp_path, monkeypatch, error, winerror, reason
):
    if winerror is not None:
        error.winerror = winerror

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(native.subprocess, "run", fail)
    with pytest.raises(ConfigError, match="execution policy") as caught:
        native._powershell_execution_policy(
            tmp_path / "Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        )
    assert reason in str(caught.value)
    assert "no sandbox command was launched" in str(caught.value)
    assert "private-query-output" not in "".join(traceback.format_exception(caught.value))


def test_each_request_preserves_fresh_host_policy(workspace, monkeypatch):
    policies = iter(("RemoteSigned", "AllSigned", "Restricted"))
    monkeypatch.setattr(native, "_powershell_execution_policy", lambda _shell: next(policies))
    for expected in ("RemoteSigned", "AllSigned", "Restricted"):
        argv = request(workspace)["argv"]
        assert argv[argv.index("-ExecutionPolicy") + 1] == expected
        assert argv[-1].endswith("Write-Output 'hello; & world'")


def test_policy_query_failure_prevents_helper_launch(workspace, monkeypatch):
    monkeypatch.setattr(native, "resolve_helper", lambda: workspace / "helper.exe")

    def fail(_shell):
        raise ConfigError("host policy unavailable")

    monkeypatch.setattr(native, "_powershell_execution_policy", fail)
    monkeypatch.setattr(
        native.subprocess, "Popen", lambda *a, **k: pytest.fail("helper was launched")
    )
    runner = native.WindowsNativeRunner(ShellSandboxSettings(backend="windows-native"))
    with pytest.raises(ConfigError, match="host policy unavailable"):
        runner.run(root=workspace, cwd=workspace, cmd="Write-Output 'never executed'", timeout_s=10)
    with pytest.raises(ConfigError, match="host policy unavailable"):
        runner.start(root=workspace, cwd=workspace, cmd="Write-Output 'never executed'")


def test_helper_integrity_and_platform_are_enforced(tmp_path, monkeypatch):
    helper = tmp_path / "alysis-sandbox.exe"
    helper.write_bytes(b"test fixture; not executable")
    monkeypatch.setattr(native.platform, "system", lambda: "Windows")
    monkeypatch.setenv("ALYSIS_WINDOWS_SANDBOX_HELPER", str(helper))
    monkeypatch.setenv(
        "ALYSIS_WINDOWS_SANDBOX_SHA256", hashlib.sha256(helper.read_bytes()).hexdigest()
    )
    calls = []

    def protocol_probe(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(_helper_metadata()), "")

    monkeypatch.setattr(native.subprocess, "run", protocol_probe)
    assert native.resolve_helper() == helper.resolve()
    helper.write_bytes(b"modified")
    with pytest.raises(ConfigError, match="integrity"):
        native.resolve_helper()
    monkeypatch.setattr(native.platform, "system", lambda: "Linux")
    with pytest.raises(ConfigError, match="WSL2"):
        native.resolve_helper()
    assert calls == [[str(helper), "protocol"]]


def _helper_metadata():
    return {
        "protocol": 3,
        "security_revision": 4,
        "implementation": "alysis-windows-sandbox",
        "containment": "appcontainer-write-restricted",
        "network_modes": ["off", "on"],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"protocol": 2},
        {"protocol": True},
        {"protocol": 3.0},
        {"security_revision": None},
        {"security_revision": 1},
        {"security_revision": 2},
        {"security_revision": 3},
        {"security_revision": 4.0},
        {"implementation": "other-helper"},
        {"containment": "restricted-token"},
        {"network_modes": ["off"]},
        {"network_modes": ["off", "on", "unrestricted"]},
    ],
)
def test_pinned_but_incompatible_helper_is_rejected(tmp_path, monkeypatch, change):
    helper = tmp_path / "old-helper.exe"
    helper.write_bytes(b"trusted but incompatible build")
    monkeypatch.setattr(native.platform, "system", lambda: "Windows")
    monkeypatch.setenv("ALYSIS_WINDOWS_SANDBOX_HELPER", str(helper))
    monkeypatch.setenv(
        "ALYSIS_WINDOWS_SANDBOX_SHA256", hashlib.sha256(helper.read_bytes()).hexdigest()
    )
    calls = []

    def incompatible(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(_helper_metadata() | change), "")

    monkeypatch.setattr(native.subprocess, "run", incompatible)
    with pytest.raises(ConfigError, match="incompatible"):
        native.resolve_helper()
    assert calls == [[str(helper), "protocol"]]


@pytest.mark.parametrize("payload", ["null", "[]", "true", "invalid json", b"\xff"])
def test_helper_json_rejects_malformed_status(monkeypatch, payload):
    monkeypatch.setattr(
        native.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, payload, ""),
    )
    with pytest.raises(ConfigError, match="status response"):
        native._helper_json(Path("helper.exe"), "status")


@pytest.mark.parametrize("error", [OSError("gone"), subprocess.TimeoutExpired("helper", 15)])
def test_helper_probe_failure_is_actionable(monkeypatch, error):
    def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(native.subprocess, "run", unavailable)
    with pytest.raises(ConfigError, match="protocol response"):
        native._verify_helper_protocol(Path("helper.exe"))


@pytest.mark.parametrize("mode", ["strict", "warn"])
@pytest.mark.parametrize("fallback", ["refuse", "ask"])
def test_missing_helper_never_falls_back(workspace, monkeypatch, mode, fallback):
    def missing():
        raise ConfigError("helper missing")

    monkeypatch.setattr(native, "resolve_helper", missing)
    settings = ShellSandboxSettings(
        backend="windows-native", mode=mode, unavailable_fallback=fallback
    )
    for factory in (build_shell_runner_from_settings, build_background_shell_runner_from_settings):
        with pytest.raises(ConfigError, match="helper missing"):
            factory(settings, workspace)


def test_native_backend_selection_never_requests_host_fallback():
    from alysis_code.sandbox_runner import SandboxBackendChoice, select_shell_sandbox_backend

    settings = ShellSandboxSettings(backend="windows-native", unavailable_fallback="ask")
    for system in ("Windows", "Linux", "Darwin"):
        choice = select_shell_sandbox_backend(
            settings, system=system, which=lambda _name: None, path_exists=lambda _path: False
        )
        assert choice == SandboxBackendChoice("windows-native")


def test_explicit_native_on_wsl_fails_even_with_approval_fallback(workspace, monkeypatch):
    monkeypatch.setattr(native.platform, "system", lambda: "Linux")
    settings = ShellSandboxSettings(backend="windows-native", unavailable_fallback="ask")
    for factory in (build_shell_runner_from_settings, build_background_shell_runner_from_settings):
        with pytest.raises(ConfigError, match="WSL2 use the Linux bwrap backend"):
            factory(settings, workspace)


def test_native_mode_off_is_explicit_host_execution(workspace, monkeypatch):
    from alysis_code.background_runner import HostBackgroundRunner
    from alysis_code.sandbox_runner import HostShellRunner

    def unexpected_probe():
        pytest.fail("off mode must not require a native helper")

    monkeypatch.setattr(native, "resolve_helper", unexpected_probe)
    settings = ShellSandboxSettings(
        backend="windows-native", mode="off", unavailable_fallback="ask"
    )
    runner = build_shell_runner_from_settings(settings, workspace)
    assert isinstance(runner, HostShellRunner)
    assert runner.requires_command_approval is False
    assert isinstance(
        build_background_shell_runner_from_settings(settings, workspace), HostBackgroundRunner
    )


def test_foreground_background_share_native_policy(workspace, monkeypatch):
    monkeypatch.setattr(native, "resolve_helper", lambda: workspace / "helper.exe")
    settings = ShellSandboxSettings(backend="windows-native")
    for factory in (build_shell_runner_from_settings, build_background_shell_runner_from_settings):
        runner = factory(settings, workspace)
        assert isinstance(runner, native.WindowsNativeRunner)
        assert runner.settings is settings


def test_desktop_filesystem_ceiling_is_consumed(monkeypatch):
    cfg = AppConfig(model="test")
    cfg.extra_fields = {"shell_sandbox": {"backend": "windows-native"}}
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_FILESYSTEM_ACCESS", "read-only")
    settings = resolve_shell_sandbox_settings(cfg)
    assert settings.backend == "windows-native"
    assert settings.filesystem_access == "read-only"


@pytest.mark.parametrize("present", [True, None])
def test_doctor_does_not_claim_ready_from_setup_presence(monkeypatch, present):
    cfg = AppConfig(model="test")
    cfg.extra_fields = {"shell_sandbox": {"backend": "windows-native"}}
    monkeypatch.setattr(
        native,
        "helper_status",
        lambda: {
            "user": {
                "user": {"exists": True},
                "cred_present": True,
                "marker_version": 3,
                "real_user_sid": "owner",
                "owner_user_sid": "owner",
            },
            "ambient": {"paths": [{"present": present}]},
        },
    )
    result = diagnose_sandbox(cfg, include_smoke=False)
    assert not result.ready
    assert result.selected_backend == "windows-native"
    assert not result.can_pull


def test_auto_on_wsl_stays_on_linux(workspace, monkeypatch):
    import alysis_code.sandbox_runner as runners

    monkeypatch.setattr(runners.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        runners.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None
    )
    assert isinstance(
        build_shell_runner_from_settings(ShellSandboxSettings(), workspace),
        runners.BwrapShellRunner,
    )
