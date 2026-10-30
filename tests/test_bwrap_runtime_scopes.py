"""Executable discovery and explicit runtime data retain mount boundaries."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from alysis_code import sandbox_runner
from alysis_code.config import ConfigError
from alysis_code.contained_read_tools import ContainedReadTools
from alysis_code.host_file_policy import HostFilePolicy, HostFilePolicyError
from alysis_code.sandbox_settings import ShellSandboxSettings


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("ALYSIS_RUN_BWRAP_RUNTIME_TESTS") != "1",
    reason="requires explicitly enabled native Linux Bubblewrap execution",
)
def test_native_worker_imports_engine_without_exposing_checkout_neighbors(scopes):
    workspace, _, private = scopes
    (workspace / "sample.txt").write_text("controlled workspace content")
    (private / "secret.txt").write_text("private canary")
    worker = ContainedReadTools(ShellSandboxSettings(backend="bwrap"), workspace)

    result = worker.run("fs_read", {"path": "sample.txt"})
    assert "controlled workspace content" in json.dumps(result)
    with pytest.raises(ConfigError):
        worker.run("fs_read", {"path": str(private / "secret.txt")})
    assert (private / "secret.txt").read_text() == "private canary"
    package = Path(sandbox_runner.__file__).resolve().parent
    checkout_metadata = package.parent.parent / "pyproject.toml"
    assert checkout_metadata.is_file()
    probe = sandbox_runner.BwrapShellRunner().run(
        root=workspace,
        cwd=workspace,
        cmd=(
            f"test -r {shlex.quote(str(package / '__init__.py'))} && "
            f"test ! -w {shlex.quote(str(package / '__init__.py'))} && "
            f"test ! -r {shlex.quote(str(checkout_metadata))}"
        ),
        timeout_s=15,
    )
    assert probe.returncode == 0, probe.stderr


@pytest.fixture
def scopes(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    workspace, runtime, private = (root / name for name in ("workspace", "runtime", "private"))
    for path in (workspace, runtime, private):
        path.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(private / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(private / "data"))
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(private / "memory"))
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(private)]))
    monkeypatch.delenv("ALYSIS_DESKTOP_USER_DATA", raising=False)
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", "[]")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_ADDITIONAL_WORKSPACE_ROOTS", "[]")
    monkeypatch.setenv("PATH", os.defpath)
    return workspace, runtime, private


def argv(workspace, **overrides):
    options = dict(
        root=workspace,
        cwd=workspace,
        cmd="true",
        network="off",
        clear_env=True,
        profile="hardened",
        unshare_cgroup=False,
    )
    options.update(overrides)
    return sandbox_runner._build_bwrap_argv(**options)[0]


def authorize(monkeypatch, *roots):
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", json.dumps([str(path) for path in roots]))


def test_path_discovers_executables_without_granting_neighboring_data(scopes, monkeypatch):
    workspace, runtime, _ = scopes
    tool = runtime / ("tool-" + uuid.uuid4().hex)
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(runtime))
    monkeypatch.setenv("CARGO_HOME", str(runtime))
    monkeypatch.setenv("RUSTUP_HOME", str(runtime))
    args = argv(workspace)
    # A PATH directory is not itself a recursive read grant.
    assert args[args.index("PATH") + 1] == str(runtime)
    assert not any(
        args[i] == "--ro-bind" and args[i + 1] == str(runtime) for i in range(len(args) - 2)
    )
    assert any(args[i : i + 3] == ["--ro-bind", str(tool), str(tool)] for i in range(len(args) - 2))
    assert "CARGO_HOME" not in args and "RUSTUP_HOME" not in args


def test_system_tool_alias_does_not_scan_or_remount_available_libraries(
    scopes, monkeypatch, require_symlinks
):
    workspace, runtime, _ = scopes
    system = workspace.parent / "system"
    binary, library = system / "bin" / "executable", system / "lib"
    binary.parent.mkdir(parents=True)
    library.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    # Hardlinks inside an already admitted system mount do not introduce
    # authority. Reinterpreting that mount as a new grant would refuse it.
    (library / "linked-system-file").hardlink_to(binary)
    alias = runtime / ("tool-" + uuid.uuid4().hex)
    alias.symlink_to(binary)
    monkeypatch.setenv("PATH", str(runtime))
    monkeypatch.setattr(sandbox_runner, "_BWRAP_SYSTEM_BIND_ROOTS", (system,))
    args = argv(workspace)
    assert any(
        args[i : i + 3] == ["--symlink", str(binary), str(alias)] for i in range(len(args) - 2)
    )
    assert str(library) not in args
    assert not any(
        args[i] == "--ro-bind" and args[i + 1] == str(binary) for i in range(len(args) - 2)
    )


def test_explicit_runtime_is_readonly_and_cannot_become_workspace(scopes, monkeypatch):
    workspace, runtime, _ = scopes
    authorize(monkeypatch, runtime)
    args = argv(workspace)
    index = args.index(str(runtime))
    assert args[index - 1 : index + 2] == ["--ro-bind", str(runtime), str(runtime)]
    with pytest.raises(ConfigError, match="trusted runtime"):
        argv(runtime)
    with pytest.raises(ConfigError, match="trusted runtime"):
        argv(workspace, additional_workspace_roots=(str(runtime),))
    with pytest.raises(HostFilePolicyError, match="protected host state or runtime"):
        HostFilePolicy.capture().guard_write(workspace, str(runtime / "tool"))


@pytest.mark.parametrize("kind", ["private", "contains-private", "inside-private", "home", "root"])
def test_broad_and_private_runtime_scopes_refuse(scopes, monkeypatch, kind):
    workspace, runtime, private = scopes
    targets = {
        "private": private,
        "contains-private": private.parent,
        "inside-private": private / "nested",
        "home": Path.home().resolve(),
        "root": Path(workspace.anchor),
    }
    target = targets[kind]
    if kind == "inside-private":
        target.mkdir()
    authorize(monkeypatch, target)
    with pytest.raises(ConfigError):
        argv(workspace)


def test_runtime_root_retargeted_to_alias_refuses(scopes, monkeypatch, require_symlinks):
    workspace, runtime, private = scopes
    authorize(monkeypatch, runtime)
    argv(workspace)
    runtime.rmdir()
    runtime.symlink_to(private, target_is_directory=True)
    with pytest.raises(ConfigError, match="runtime policy"):
        argv(workspace)


def test_runtime_hardlink_cannot_expose_private_file(scopes, monkeypatch):
    workspace, runtime, private = scopes
    secret = private / "canary"
    secret.write_text("synthetic-private")
    os.link(secret, runtime / "ordinary-tool-data")
    authorize(monkeypatch, runtime)
    with pytest.raises(ConfigError, match="hardlinked files"):
        argv(workspace)
    assert secret.read_text() == "synthetic-private"


def test_additional_workspace_cannot_mount_separate_memory_home(scopes, monkeypatch):
    workspace, _, _ = scopes
    memory = workspace.parent / "separate-memory"
    memory.mkdir()
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(memory))
    with pytest.raises(ConfigError, match="private state"):
        argv(workspace, additional_workspace_roots=(str(memory),))


@pytest.mark.parametrize("cause", ["stop", "deadline"])
def test_runtime_inspection_obeys_stop_and_deadline_before_launch(scopes, monkeypatch, cause):
    workspace, runtime, _ = scopes
    (runtime / "payload").write_text("unchanged")
    authorize(monkeypatch, runtime)
    real_scandir = sandbox_runner.os.scandir
    scanning = False

    def scandir(path):
        nonlocal scanning
        scanning = True
        return real_scandir(path)

    monkeypatch.setattr(sandbox_runner.os, "scandir", scandir)
    monkeypatch.setattr(sandbox_runner, "_supports_bwrap_unshare_cgroup", lambda: False)
    if cause == "deadline":
        monkeypatch.setattr(sandbox_runner.time, "monotonic", lambda: 6 if scanning else 0)

    def unexpected_launch(*args, **kwargs):
        pytest.fail("A cancelled or expired preparation must not launch a process")

    monkeypatch.setattr(sandbox_runner, "run_in_tracked_process_group", unexpected_launch)
    expected = (
        sandbox_runner.CommandCancelledError if cause == "stop" else subprocess.TimeoutExpired
    )
    with pytest.raises(expected):
        sandbox_runner.BwrapShellRunner().run(
            root=workspace,
            cwd=workspace,
            cmd="never launched",
            timeout_s=5,
            cancel_check=lambda: scanning and cause == "stop",
        )
    assert scanning


def test_future_runtime_scope_stays_protected_without_mounting_missing_source(scopes, monkeypatch):
    workspace, runtime, _ = scopes
    future = runtime / "next-generation"
    authorize(monkeypatch, future)
    args = argv(workspace)
    assert str(future) not in args
    assert not future.exists()
    with pytest.raises(HostFilePolicyError):
        HostFilePolicy.capture().guard_write(workspace, str(future))


@pytest.mark.parametrize(
    "overrides", [{"clear_env": False}, {"profile": "compat"}, {"protect_repo_meta": False}]
)
def test_runtime_mounts_require_hardened_policy(scopes, monkeypatch, overrides):
    workspace, runtime, _ = scopes
    authorize(monkeypatch, runtime)
    with pytest.raises(ConfigError, match="hardened sandbox policy"):
        argv(workspace, **overrides)


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("ALYSIS_RUN_BWRAP_RUNTIME_TESTS") != "1",
    reason="requires explicitly enabled native Linux Bubblewrap execution",
)
@pytest.mark.parametrize("read_only", [False, True])
def test_native_arbitrary_tool_dependency_and_shim_keep_scope_boundaries(
    scopes,
    monkeypatch,
    read_only,
    require_symlinks,
):
    workspace, runtime, private = scopes
    installation = runtime / "tool installation Δ 資料"
    executable_dir = installation / "executables"
    executable_dir.mkdir(parents=True)
    library = installation / "support"
    library.mkdir()
    token = uuid.uuid4().hex
    payload = library / "payload"
    payload.write_text(token)
    name = "tool-" + uuid.uuid4().hex
    executable = executable_dir / name
    executable.write_text("#!/bin/sh\ncat " + shlex.quote(str(payload)) + "\n")
    executable.chmod(0o755)
    shims = runtime / "shims"
    shims.mkdir()
    (shims / name).symlink_to(executable)
    canary = private / "canary"
    canary.write_text("private-unchanged")
    # An executable symlink in an admitted scope must not import its target's authority.
    (shims / "private-alias").symlink_to(canary)
    sibling = runtime / "unrelated.txt"
    sibling.write_text("sibling-unchanged")
    monkeypatch.setenv("PATH", os.pathsep.join((str(shims), "/usr/bin", "/bin")))
    runner = sandbox_runner.BwrapShellRunner(
        filesystem_access="read-only" if read_only else "workspace-write",
    )

    def run(command):
        return runner.run(root=workspace, cwd=workspace, cmd=command, timeout_s=15)

    assert run(name).returncode != 0, "PATH discovery must not admit arbitrary support data"
    authorize(monkeypatch, shims)
    assert run(name).returncode != 0, "A shim must not grant an unapproved target tree"
    authorize(monkeypatch, shims, installation)
    result = run(name)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == token
    # Exercise a child shell and an absolute executable path as well as PATH lookup.
    assert run("sh -c " + shlex.quote(shlex.quote(str(executable)))).stdout == token
    for target in (sibling, canary, shims / "private-alias"):
        assert run("cat " + shlex.quote(str(target))).returncode != 0
    assert run("printf changed > " + shlex.quote(str(payload))).returncode != 0
    write = run("printf workspace > created.txt")
    assert (write.returncode != 0) == read_only
    if not read_only:
        assert (workspace / "created.txt").read_text() == "workspace"
    authorize(monkeypatch)
    assert run(name).returncode != 0, "Removing a runtime scope must affect the next launch"
    assert payload.read_text() == token
    assert canary.read_text() == "private-unchanged"
    assert sibling.read_text() == "sibling-unchanged"


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("ALYSIS_RUN_BWRAP_RUNTIME_TESTS") != "1",
    reason="requires explicitly enabled native Linux Bubblewrap execution",
)
def test_native_discovery_preserves_executable_alias_identity(scopes, monkeypatch):
    workspace, runtime, _ = scopes
    node = shutil.which("node")
    if not node:
        pytest.skip("requires installed Node")
    binary = runtime / "bin" / ("tool-" + uuid.uuid4().hex)
    binary.parent.mkdir()
    shutil.copyfile(node, binary)
    binary.chmod(0o755)
    alias = runtime / "shims" / binary.name
    alias.parent.mkdir()
    alias.symlink_to(binary)
    neighbor = alias.parent / "private-data"
    neighbor.write_text("synthetic-private")
    monkeypatch.setenv("PATH", str(alias.parent) + os.pathsep + os.defpath)
    runner = sandbox_runner.BwrapShellRunner()
    result = runner.run(
        root=workspace, cwd=workspace, cmd=binary.name + " -p process.execPath", timeout_s=15
    )
    assert result.returncode == 0 and result.stdout.strip() == str(binary), result.stderr
    denied = runner.run(
        root=workspace, cwd=workspace, cmd="cat " + shlex.quote(str(neighbor)), timeout_s=15
    )
    assert denied.returncode != 0


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("ALYSIS_RUN_BWRAP_RUNTIME_TESTS") != "1",
    reason="requires explicitly enabled native Linux Bubblewrap execution",
)
def test_native_alias_to_existing_system_tool(scopes, monkeypatch):
    workspace, runtime, _ = scopes
    # Some cat implementations dispatch by argv[0] and reject arbitrary aliases.
    executable = shutil.which("sh")
    assert executable
    alias = runtime / ("tool-" + uuid.uuid4().hex)
    alias.symlink_to(executable)
    (workspace / "payload").write_text("system-alias-ok")
    monkeypatch.setenv("PATH", str(runtime) + os.pathsep + os.defpath)
    result = sandbox_runner.BwrapShellRunner().run(
        root=workspace, cwd=workspace, cmd=alias.name + ' -c "cat payload"', timeout_s=15
    )
    assert result.returncode == 0 and result.stdout == "system-alias-ok", result.stderr


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("ALYSIS_RUN_BWRAP_RUNTIME_TESTS") != "1",
    reason="requires explicitly enabled native Linux Bubblewrap execution",
)
@pytest.mark.parametrize(
    "layout", ["system-executable", "system-directory", "code-directory", "both"]
)
def test_native_external_alias_and_linked_library_keep_scope_boundaries(
    scopes, monkeypatch, layout
):
    workspace, runtime, private = scopes
    binaries = runtime / "bin"
    binaries.mkdir()
    library = runtime / "lib"
    if layout in {"code-directory", "both"}:
        target = runtime / "lib-platform"
        target.mkdir()
        library.symlink_to(target.name, target_is_directory=True)
    else:
        library.mkdir()
    payload = library / "payload"
    payload.write_text("linked-runtime-ok")
    neighbor = runtime / "unapproved"
    neighbor.write_text("private-unchanged")
    canary = private / "canary"
    canary.write_text("private-unchanged")
    (library / "private-link").symlink_to(canary)
    tool = binaries / ("tool-" + uuid.uuid4().hex)
    tool.write_text("#!/bin/sh\ncat " + shlex.quote(str(payload)) + "\n")
    tool.chmod(0o755)
    path_directory = binaries
    if layout in {"system-executable", "system-directory", "both"}:
        # Treat this fixture as an already mounted interpreter installation.
        # Real Bubblewrap mounts it through the existing Python runtime path.
        # Its executable alias points outside that mount, like /usr/local/bin.
        system = workspace.parent / "mounted-system"
        path_directory = system / "bin"
        system.mkdir()
        if layout == "system-directory":
            path_directory.symlink_to(binaries, target_is_directory=True)
        else:
            path_directory.mkdir()
            (path_directory / tool.name).symlink_to(tool)
        roots = sandbox_runner._python_runtime_ro_bind_roots()
        monkeypatch.setattr(
            sandbox_runner, "_python_runtime_ro_bind_roots", lambda: (*roots, system)
        )
    monkeypatch.setenv("PATH", str(path_directory) + os.pathsep + os.defpath)
    runner = sandbox_runner.BwrapShellRunner()

    def run(command):
        return runner.run(root=workspace, cwd=workspace, cmd=command, timeout_s=15)

    result = run(tool.name)
    assert result.returncode == 0 and result.stdout == "linked-runtime-ok", result.stderr
    for denied in (neighbor, canary, library / "private-link"):
        assert run("cat " + shlex.quote(str(denied))).returncode != 0
    assert run("printf changed > " + shlex.quote(str(payload))).returncode != 0
    assert payload.read_text() == "linked-runtime-ok"


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("ALYSIS_RUN_BWRAP_RUNTIME_TESTS") != "1",
    reason="requires explicitly enabled native Linux Bubblewrap execution",
)
def test_native_unknown_tool_loads_adjacent_code_without_reading_installation_siblings(
    scopes, monkeypatch
):
    workspace, runtime, _ = scopes
    binaries, library = runtime / "bin", runtime / "lib"
    binaries.mkdir()
    library.mkdir()
    (library / "data").write_text("code-data")
    (runtime / "private-data").write_text("unapproved")
    tool = binaries / ("tool-" + uuid.uuid4().hex)
    tool.write_text('#!/bin/sh\ncat "$(dirname "$0")/../lib/data"\n')
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.defpath)
    runner = sandbox_runner.BwrapShellRunner()
    result = runner.run(root=workspace, cwd=workspace, cmd=tool.name, timeout_s=15)
    assert result.returncode == 0 and result.stdout == "code-data", result.stderr
    denied = runner.run(
        root=workspace,
        cwd=workspace,
        cmd="cat " + shlex.quote(str(runtime / "private-data")),
        timeout_s=15,
    )
    assert denied.returncode != 0


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("ALYSIS_RUN_BWRAP_RUNTIME_TESTS") != "1",
    reason="requires explicitly enabled native Linux Bubblewrap execution",
)
@pytest.mark.parametrize("extended", [False, True])
def test_native_workspace_symlinks_stay_inside_mount_authority(scopes, monkeypatch, extended):
    workspace, runtime, private = scopes
    (workspace / "payload").write_text("workspace-data")
    (workspace / "local-link").symlink_to("payload")
    (private / "payload").write_text("private-data")
    (workspace / "outside-link").symlink_to(private / "payload")
    (workspace / "outside-directory").symlink_to(private, target_is_directory=True)
    if extended:
        authorize(monkeypatch, runtime)
    runner = sandbox_runner.BwrapShellRunner()

    def run(cmd):
        return runner.run(root=workspace, cwd=workspace, cmd=cmd, timeout_s=15)

    assert run("cat local-link").stdout == "workspace-data"
    assert run("cat outside-link").returncode != 0
    assert run("cat outside-directory/payload").returncode != 0
    assert run("printf changed > outside-link").returncode != 0
    # A pre-existing hardlink cannot smuggle the same private inode into a grant.
    (workspace / "hardlink").hardlink_to(private / "payload")
    with pytest.raises(ConfigError, match="hardlinked"):
        run("cat hardlink")
    assert (private / "payload").read_text() == "private-data"


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("ALYSIS_RUN_BWRAP_RUNTIME_TESTS") != "1",
    reason="requires explicitly enabled native Linux Bubblewrap execution",
)
@pytest.mark.parametrize("name", [".git", ".alysis", "AGENTS.md"])
def test_native_metadata_overlay_cannot_import_private_alias(scopes, monkeypatch, name):
    workspace, runtime, private = scopes
    payload = private / "payload"
    payload.write_text("private-unchanged")
    source = payload if name == "AGENTS.md" else private
    (workspace / name).symlink_to(source, target_is_directory=source.is_dir())
    authorize(monkeypatch, runtime)
    runner = sandbox_runner.BwrapShellRunner()
    with pytest.raises(ConfigError, match="metadata.*symlink"):
        runner.run(root=workspace, cwd=workspace, cmd="cat " + name, timeout_s=15)
    assert payload.read_text() == "private-unchanged"


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("ALYSIS_RUN_BWRAP_RUNTIME_TESTS") != "1",
    reason="requires explicitly enabled native Linux Bubblewrap execution",
)
def test_native_git_writes_with_runtime_and_extra_workspace_keep_metadata_protected(
    scopes, monkeypatch
):
    workspace, runtime, _ = scopes
    extra = workspace.parent / "extra"
    extra.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True, capture_output=True)
    for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.invalid")):
        subprocess.run(["git", "-C", str(workspace), "config", key, value], check=True)
    (workspace / "change.txt").write_text("reviewed change")
    authorize(monkeypatch, runtime)
    runner = sandbox_runner.BwrapShellRunner(additional_workspace_roots=(str(extra),))

    def run(cmd):
        return runner.run(root=workspace, cwd=workspace, cmd=cmd, timeout_s=15)

    assert run("git add change.txt").returncode == 0
    commit = run("git commit -m 'fixture commit'")
    assert commit.returncode == 0, commit.stderr
    config = (workspace / ".git/config").read_bytes()
    assert run("printf unreviewed > .git/config").returncode != 0
    assert run("printf unreviewed > " + shlex.quote(str(workspace / ".git/config"))).returncode != 0
    assert (workspace / ".git/config").read_bytes() == config
