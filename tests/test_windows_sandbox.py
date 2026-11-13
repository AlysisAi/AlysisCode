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
        cmd=kwargs.pop("cmd", "Write-Output 'hello; & world'"),
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
    assert result["argv"][-1].endswith(";\nWrite-Output 'hello; & world'")
    assert "-NoProfile" in result["argv"]


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell bootstrap")
def test_bootstrap_loads_standard_cmdlets_without_module_discovery(workspace, monkeypatch):
    # Exercise the generated command in real Windows PowerShell. An isolated
    # module analysis cache must not make first-use command discovery exceed
    # the normal command deadline. The installed suite repeats this under the
    # restricted token; this control isolates the PowerShell startup itself.
    monkeypatch.setenv("PSModulePath", str(workspace / "untrusted-modules"))
    nested = workspace / "nested's folder"
    nested.mkdir()
    result = request(workspace, cwd=nested)
    result["argv"][-1] += (
        "; [Console]::Out.WriteLine((Get-Location).Path); "
        "[Console]::Out.WriteLine((Get-ExecutionPolicy).ToString())"
    )
    environment = dict(result["env"])
    environment["PSModuleAnalysisCachePath"] = str(workspace / "fresh-module-cache")
    completed = subprocess.run(
        result["argv"],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert not completed.stderr
    assert completed.stdout.splitlines() == [
        "hello; & world",
        "Alysis:\\nested's folder",
        "RemoteSigned",
    ]


@pytest.mark.skipif(os.name != "nt", reason="request construction needs local NTFS paths")
def test_repository_kept_in_another_folder_is_protected(workspace):
    """`git init --separate-git-dir` leaves a .git file; the repository has another name."""
    repository = workspace / "meta"
    repository.mkdir()
    (workspace / ".git").write_text("gitdir: meta\n")
    denied = [Path(path) for path in request(workspace)["deny_write"]]
    assert repository in denied and workspace / ".git" in denied
    assert request(workspace)["write"] == [str(workspace)]


@pytest.mark.skipif(os.name != "nt", reason="request construction needs local NTFS paths")
@pytest.mark.parametrize("pointer", ["gitdir: .\n", "gitdir:\n", "gitdir: ..\n", "not a pointer\n"])
def test_git_pointer_never_protects_the_workspace_or_a_folder_above_it(workspace, pointer):
    (workspace / ".git").write_text(pointer)
    result = request(workspace)
    assert result["write"] == [str(workspace)]
    denied = [Path(path) for path in result["deny_write"]]
    assert workspace not in denied and workspace.parent not in denied


@pytest.mark.skipif(os.name != "nt", reason="request construction needs local NTFS paths")
def test_repository_kept_under_a_folder_named_git_is_protected(workspace):
    """Only exact paths are protected here, so a .git elsewhere in the path covers nothing."""
    repository = workspace / "store" / ".git"
    repository.mkdir(parents=True)
    (workspace / ".git").write_text("gitdir: store/.git\n")
    denied = [Path(path) for path in request(workspace)["deny_write"]]
    assert repository in denied


@pytest.mark.skipif(os.name != "nt", reason="request construction needs local NTFS paths")
def test_shared_repository_of_a_worktree_inside_the_project_is_protected(workspace):
    shared = workspace / "main" / ".git"
    entry = shared / "worktrees" / "wt"
    entry.mkdir(parents=True)
    (entry / "commondir").write_text("../..\n")
    (workspace / ".git").write_text("gitdir: main/.git/worktrees/wt\n")
    denied = [Path(path) for path in request(workspace)["deny_write"]]
    assert shared in denied
    assert any(entry.is_relative_to(path) for path in denied)


@pytest.mark.skipif(os.name != "nt", reason="request construction needs local NTFS paths")
def test_git_directory_that_is_an_approved_folder_is_reported_not_skipped(workspace):
    other = workspace.parent / "other"
    other.mkdir()
    (workspace / ".git").write_text("gitdir: ../other\n")
    settings = ShellSandboxSettings(
        backend="windows-native", additional_workspace_roots=(str(other),)
    )
    assert native.pointed_git_directories([workspace, other]) == ([], [other])
    assert native.git_directory_left_writable(workspace, settings)
    assert not native.git_directory_left_writable(workspace, ShellSandboxSettings())
    # The request still builds: the folder was approved for writing by a person.
    assert str(other) in request(workspace, settings)["write"]


def test_powershell_literal_doubles_every_quote_powershell_accepts():
    assert native.powershell_literal("a'b") == "a''b"
    assert native.powershell_literal("a‘b’c‚d‛e") == ("a‘‘b’’c‚‚d‛‛e")
    assert native.powershell_literal('plain "x" `y $z') == 'plain "x" `y $z'


@pytest.mark.skipif(os.name != "nt", reason="runs Windows PowerShell on the generated source")
@pytest.mark.parametrize("quote", ["'", "‘", "’", "‚", "‛"])
def test_folder_name_cannot_add_statements_to_the_command(workspace, quote):
    """PowerShell closes a single-quoted string at typographic quotes too."""
    marker = workspace / "added-statement.txt"
    folder = (
        workspace / f"pkg{quote}; Set-Content -LiteralPath added-statement.txt -Value x; {quote}"
    )
    folder.mkdir()
    source = request(workspace, cwd=folder)["argv"][-1]
    completed = subprocess.run(
        ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", source],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "hello; & world" in completed.stdout, completed.stderr
    assert not marker.exists() and not (folder / "added-statement.txt").exists()


def test_the_command_starts_on_a_line_of_its_own(workspace):
    source = request(workspace, cmd="npm test\nnpm run build")["argv"][-1]
    preparation, command = source.split("\n", 1)
    assert command == "npm test\nnpm run build"
    # The built-in modules are loaded by path first, then the drive is made.
    assert preparation.startswith("Microsoft.PowerShell.Core\\Import-Module ")
    assert "New-PSDrive -Name Alysis" in preparation
    assert preparation.endswith("-ErrorAction Stop;")


@pytest.mark.skipif(os.name != "nt", reason="runs Windows PowerShell on the generated source")
def test_a_mistake_in_the_command_is_reported_without_the_preparation(workspace):
    """PowerShell quotes the line an error is on; that line is the command alone."""
    mistake = "Write-Output one && Write-Output two"
    source = request(workspace, cmd=mistake)["argv"][-1]
    completed = subprocess.run(
        ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", source],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode != 0 and "one" not in completed.stdout
    assert "+ " + mistake in completed.stderr
    assert "New-PSDrive" not in completed.stderr and "Set-Location" not in completed.stderr


@pytest.mark.skipif(os.name != "nt", reason="runs Windows PowerShell on the generated source")
def test_expanded_file_name_cannot_add_statements_to_a_verifier_command(tmp_path):
    from alysis_code.windows_verification_command import quote_windows_verifier_parts

    name = "x’; Set-Content -LiteralPath added-statement.txt -Value x; ‘.py"
    source = quote_windows_verifier_parts(["cmd.exe", "/c", "echo", name])
    completed = subprocess.run(
        ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", source],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert not (tmp_path / "added-statement.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="request construction needs local NTFS paths")
def test_repository_outside_every_approved_folder_adds_no_protection(workspace):
    # A linked worktree: the repository is elsewhere and already unwritable.
    outside = workspace.parent / "main-repository"
    outside.mkdir()
    (workspace / ".git").write_text(f"gitdir: {outside}\n")
    denied = [Path(path) for path in request(workspace)["deny_write"]]
    assert outside not in denied


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


def test_a_sandbox_that_decides_by_path_takes_a_folder_with_links_in_it(workspace):
    """A uv environment and pnpm's node_modules are full of hard-linked files.

    The legacy helper changes permissions, which a hard-linked file shares
    among all its names. The MXC sandbox decides by path, and refusing the
    folder meant that no command could start in such a project.
    """
    cache = workspace.parent / "cache.bin"
    cache.write_text("cached package")
    (workspace / "package.bin").hardlink_to(cache)
    (workspace / "a.txt").write_text("x")
    (workspace / "a-again.txt").hardlink_to(workspace / "a.txt")
    with pytest.raises(ConfigError, match="hardlinked files"):
        request(workspace)
    result = request(workspace, validate_trees=False)
    assert result["write"] == [str(workspace)]


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md", "CONVENTIONS.md"])
def test_an_instruction_file_with_a_second_name_is_still_refused(workspace, name):
    """The command could change the file through the name that is not protected."""
    (workspace / name).write_text("the person's instructions")
    (workspace / "notes.md").hardlink_to(workspace / name)
    with pytest.raises(ConfigError, match="has another name"):
        request(workspace, validate_trees=False)
    (workspace / "notes.md").unlink()
    assert str(workspace / name) in request(workspace, validate_trees=False)["deny_write"]


def test_no_names_are_reserved_in_a_folder_that_is_only_let_written(workspace):
    """What exists of those names there stays read-only; nothing is made up."""
    reports = workspace.parent / "Reports"
    attached = workspace.parent / "attached"
    for folder in (reports, attached):
        folder.mkdir()
    (reports / ".git").mkdir()
    (reports / "AGENTS.md").write_text("the person's own")
    settings = ShellSandboxSettings(
        backend="windows-native",
        additional_workspace_roots=(str(reports), str(attached)),
        windows_plain_write_roots=(str(reports),),
    )
    result = request(workspace, settings, validate_trees=False)
    assert result["write"] == [str(workspace), str(reports), str(attached)]
    in_reports = sorted(value for value in result["deny_write"] if Path(value).parent == reports)
    assert in_reports == [str(reports / ".git"), str(reports / "AGENTS.md")]
    # The project and the attached folder keep every reserved name.
    for project in (workspace, attached):
        listed = {Path(value.rstrip("\\/")).name for value in result["deny_write"]}
        assert {"CLAUDE.md", ".claude", ".git"} <= listed
        assert str(project / "CLAUDE.md") in result["deny_write"]
        assert str(project / ".claude") + os.sep in result["deny_write"]


def test_the_commands_location_is_the_folder_itself_where_the_sandbox_allows(workspace):
    """On the "Alysis:" drive "$PWD" was a path .NET refuses, and the folder above was the folder."""
    nested = workspace / "it's nested"
    nested.mkdir()
    built = request(workspace, cwd=nested, cmd="Get-Location", real_location=True)
    script = built["argv"][-1]
    # Both ways are handed to the caller: whether PowerShell can stand in the
    # folder is known only when the whole request is.
    in_the_folder, on_the_drive = built["location"]
    assert script.count(in_the_folder) == 1 and "New-PSDrive -Name Alysis" in on_the_drive
    assert "location" not in request(workspace, cwd=nested)
    preparation, command = script.split("\n", 1)
    assert preparation.startswith("Microsoft.PowerShell.Core\\Import-Module ")
    assert preparation.endswith(
        "Set-Location -LiteralPath '" + str(nested).replace("'", "''") + "' -ErrorAction Stop;"
    )
    # The caller swaps one for the other when PowerShell cannot stand in the folder.
    swapped = script.replace(in_the_folder, on_the_drive, 1)
    assert swapped.endswith("\nGet-Location") and "New-PSDrive -Name Alysis" in swapped
    assert command == "Get-Location" and "Alysis:" not in script and "New-PSDrive" not in script
    # The legacy helper cannot see the folders above and keeps the drive.
    assert "New-PSDrive -Name Alysis" in request(workspace, cwd=nested)["argv"][-1]


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
        "PSModulePath",
    ):
        monkeypatch.setenv(name, "never-forward-this")
    assert "never-forward-this" not in json.dumps(request(workspace)["env"])
    assert dict(request(workspace)["env"])["NODE_OPTIONS"] == (
        "--preserve-symlinks --preserve-symlinks-main"
    )
    result = request(workspace)
    assert dict(result["env"])["PSMODULEPATH"] == str(Path(result["argv"][0]).parent / "Modules")
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
        "security_revision": 6,
        "implementation": "alysis-windows-sandbox",
        "containment": "lpac-write-restricted",
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
        {"security_revision": 4},
        {"security_revision": 5},
        {"security_revision": 6.0},
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
                "marker_version": 4,
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


# ---- repositories stay writable for Git while a command runs -----------------


def _repository_at(folder: Path) -> Path:
    (folder / "objects").mkdir(parents=True)
    (folder / "HEAD").write_text("ref: refs/heads/main\n")
    return folder


def test_a_folder_named_git_that_holds_a_repository_is_one(workspace):
    repository = _repository_at(workspace / ".git")
    assert native.repository_directories([workspace]) == [repository]


def test_an_empty_git_folder_that_only_reserves_the_name_is_not_a_repository(workspace):
    (workspace / ".git").mkdir()
    assert native.repository_directories([workspace]) == []


def test_a_repository_behind_a_git_file_is_found_inside_the_project(workspace):
    repository = _repository_at(workspace / "meta")
    (workspace / ".git").write_text("gitdir: meta\n")
    assert native.repository_directories([workspace.resolve()]) == [repository.resolve()]


def test_a_repository_outside_every_approved_folder_is_not_the_commands_to_free(workspace):
    outside = _repository_at(workspace.parent / "store" / "repo")
    (workspace / ".git").write_text(f"gitdir: {outside.as_posix()}\n")
    assert native.repository_directories([workspace.resolve()]) == []


def _freed(workspace, *, person: bool, held=None):
    from alysis_code import windows_mxc as mxc

    protected = [str(workspace / name) for name in (".git", ".claude", "AGENTS.md")]
    request = {"read": [str(workspace)], "write": [str(workspace)], "deny_write": list(protected)}
    token = mxc._person_command.set(person)
    held_token = mxc._held_repositories.set(held)
    try:
        mxc._free_repositories(request, [str(workspace)])
    finally:
        mxc._held_repositories.reset(held_token)
        mxc._person_command.reset(token)
    return request


def test_a_repository_is_given_to_the_helper_as_a_rule_and_not_to_hold(workspace):
    """Held, it cannot be written by Git either: every rename into it fails."""
    _repository_at(workspace / ".git")
    (workspace / ".claude").mkdir()
    request = _freed(workspace, person=True)
    assert request["deny_write"] == [str(workspace / ".claude"), str(workspace / "AGENTS.md")]
    assert request["read"] == [str(workspace), str(workspace / ".git")]
    # Still nothing the command may write: the rule for it is read-only.
    assert request["write"] == [str(workspace)]


def test_a_reserved_git_name_without_a_repository_stays_held(workspace):
    (workspace / ".git").mkdir()
    request = _freed(workspace, person=True)
    assert str(workspace / ".git") in request["deny_write"]
    assert request["read"] == [str(workspace)]


def test_the_engines_fixed_workers_decide_about_repositories_themselves(workspace):
    _repository_at(workspace / ".git")
    request = _freed(workspace, person=False)
    assert str(workspace / ".git") in request["deny_write"]
    assert request["read"] == [str(workspace)]


def test_a_background_command_frees_only_the_repositories_the_engine_holds(workspace):
    """One that appeared since, or could not be opened, stays the helper's to hold."""
    repository = _repository_at(workspace / ".git")
    held = _freed(workspace, person=True, held=frozenset({str(repository)}))
    assert str(repository) in held["read"] and str(repository) not in held["deny_write"]
    not_held = _freed(workspace, person=True, held=frozenset())
    assert str(repository) in not_held["deny_write"] and not_held["read"] == [str(workspace)]


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing")
def test_a_directory_that_cannot_be_opened_is_reported_as_not_held(tmp_path):
    from alysis_code._windows_git_reservations import HeldDirectories

    present = tmp_path / ".git"
    present.mkdir()
    held = HeldDirectories([present, tmp_path / "missing" / ".git"])
    try:
        assert held.paths == [present]
    finally:
        held.close()
    assert held.paths == []


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing")
def test_a_held_directory_cannot_go_away_and_can_still_be_written(tmp_path):
    from alysis_code._windows_git_reservations import HeldDirectories

    repository = tmp_path / ".git"
    repository.mkdir()
    (repository / "index.lock").write_text("new index")
    held = HeldDirectories([repository])
    try:
        with pytest.raises(PermissionError):
            repository.rename(tmp_path / "gone")
        # What Git does to replace its index: a rename into the directory.
        os.replace(repository / "index.lock", repository / "index")
        (repository / "COMMIT_EDITMSG").write_text("message")
        assert (repository / "index").read_text() == "new index"
    finally:
        held.close()
    repository.rename(tmp_path / "moved")
    assert (tmp_path / "moved" / "index").is_file()
