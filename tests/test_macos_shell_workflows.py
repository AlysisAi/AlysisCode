"""Real command workflows, including permission recovery and runtime grants."""

import json
import os
import shlex
import shutil
import sys
import uuid

import pytest

from alysis_code.agent.scoped_access import ScopedAccess
from alysis_code.host_file_policy import HostFilePolicy
from alysis_code.sandbox_runner import SeatbeltShellRunner, build_shell_runner_from_settings
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.surface import ApprovalDecision
from alysis_code.tools.shell import shell_run

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="real macOS Seatbelt required")


@pytest.fixture
def scope(tmp_path, monkeypatch):
    project, runtime, private = (
        tmp_path.resolve() / name for name in ("project", "runtime", "private")
    )
    for path in (project, runtime, private):
        path.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(private / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(private / "data"))
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(private / "memory"))
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(private)]))
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", json.dumps([str(runtime)]))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    return project, runtime


def run(project, cmd, runner=None):
    return shell_run(root=project, cmd=cmd, runner=runner or SeatbeltShellRunner(), timeout_s=30)


def test_denied_interpreter_does_not_repeat_side_effects_or_run_replacement(scope, monkeypatch):
    project, _ = scope
    binaries = project / "bin"
    binaries.mkdir()
    denied = binaries / "python"
    denied.write_text("#!/bin/sh\nprintf 'python: Permission denied\\n' >&2\nexit 126\n")
    replacement = binaries / "python3"
    replacement.write_text("#!/bin/sh\nprintf unexpected > replacement-ran\n")
    for binary in (denied, replacement):
        binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.defpath)
    cmd = "printf 'once\\n' >> calls; python task.py"
    result = run(project, cmd)
    assert result["exit_code"] == 126, result
    assert result["effective_cmd"] == cmd
    assert "python: Permission denied" in result["stderr"]
    assert (project / "calls").read_text() == "once\n"
    assert not (project / "replacement-ran").exists()


def test_npm_created_executable_link_works_with_runtime_scope(scope):
    if not shutil.which("npm") or not shutil.which("node"):
        pytest.skip("requires installed npm and Node")
    project, _ = scope
    package = project / "package"
    package.mkdir()
    (package / "package.json").write_text(
        json.dumps({"name": "fixture-cli", "version": "1.0.0", "bin": {"fixture-cli": "cli.js"}})
    )
    (package / "cli.js").write_text("#!/usr/bin/env node\nconsole.log('installed-cli-ok');\n")
    (package / "cli.js").chmod(0o755)
    (project / "package.json").write_text(
        json.dumps({"private": True, "dependencies": {"fixture-cli": "file:./package"}})
    )
    installed = run(
        project,
        "npm install --offline --ignore-scripts --no-audit --no-fund --cache .npm-cache",
    )
    assert installed["exit_code"] == 0, installed
    assert (project / "node_modules/.bin/fixture-cli").is_symlink()
    result = run(project, "./node_modules/.bin/fixture-cli")
    assert result["exit_code"] == 0 and "installed-cli-ok" in result["stdout"], result


@pytest.mark.parametrize("absolute_link", [False, True])
def test_discovered_library_alias_retains_read_and_write_boundaries(
    scope, monkeypatch, absolute_link
):
    project, runtime = scope
    binaries = runtime / "bin"
    binaries.mkdir()
    target = runtime / "lib-platform"
    target.mkdir()
    library = runtime / "lib"
    library.symlink_to(target if absolute_link else target.name, target_is_directory=True)
    payload = library / "payload"
    payload.write_text("linked-runtime-ok")
    neighbor = runtime / "unapproved"
    neighbor.write_text("private-unchanged")
    tool = binaries / ("tool-" + uuid.uuid4().hex)
    tool.write_text("#!/bin/sh\ncat " + shlex.quote(str(payload)) + "\n")
    tool.chmod(0o755)
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", "[]")
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.defpath)
    result = run(project, tool.name)
    assert result["exit_code"] == 0 and result["stdout"] == "linked-runtime-ok", result
    assert run(project, "cat " + shlex.quote(str(neighbor)))["exit_code"] != 0
    assert run(project, "printf changed > " + shlex.quote(str(payload)))["exit_code"] != 0
    assert payload.read_text() == "linked-runtime-ok"


def test_git_commit_with_runtime_scope_keeps_ordinary_metadata_writes_blocked(scope):
    project, _ = scope
    (project / "change.txt").write_text("reviewed change\n")
    result = run(
        project,
        "git init && git config user.name 'Fixture Author' && "
        "git config user.email 'fixture@example.invalid' && "
        "git add change.txt && git commit -m 'fixture commit'",
    )
    assert result["exit_code"] == 0, result
    status = run(project, "git status --porcelain")
    assert status["exit_code"] == 0 and not status["stdout"].strip(), status
    config = (project / ".git/config").read_bytes()
    assert run(project, "printf unreviewed > .git/config")["exit_code"] != 0
    assert (project / ".git/config").read_bytes() == config


def test_git_children_and_read_worker_share_private_config_without_metadata_authority(
    scope, monkeypatch
):
    from alysis_code.contained_read_tools import ContainedReadTools

    project, _ = scope
    (project / "change.txt").write_text("first\n")
    assert (
        run(
            project,
            "git init && git config user.name Fixture && "
            "git config user.email fixture@example.invalid && git add change.txt && "
            "git commit -m first && git commit --allow-empty -m second",
        )["exit_code"]
        == 0
    )
    config = project.parent / "private/preferences"
    config.write_text("[user]\nname = Projected User\n[credential]\nhelper = excluded\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    for command in [
        "git diff HEAD~1 HEAD --stat",
        "git log -1 --format=%G?",
        "sh -c 'git diff HEAD~1 HEAD --stat'",
    ]:
        result = run(project, command)
        assert result["exit_code"] == 0, result
    result = run(project, "sh -c 'git config --global --get user.name'")
    assert result["exit_code"] == 0 and result["stdout"].strip() == "Projected User", result
    assert run(project, "sh -c 'git config --global --get credential.helper'")["exit_code"] == 1
    worker = ContainedReadTools(ShellSandboxSettings(backend="seatbelt"), project)
    assert "status" in worker.run("git_status", {})
    original = (project / ".git/config").read_bytes()
    assert run(project, "sh -c 'git config user.name Unreviewed'")["exit_code"] != 0
    assert (project / ".git/config").read_bytes() == original
    assert run(project, "cat " + shlex.quote(str(config)))["exit_code"] != 0


def test_unknown_tool_recovers_with_exact_read_grant_that_expires(scope, monkeypatch):
    project, _ = scope
    installation = project.parent / "installation Δ 資料"
    binaries, resources = installation / "bin", installation / "resources"
    binaries.mkdir(parents=True)
    resources.mkdir()
    (resources / "data").write_text("runtime-data")
    neighbor = installation / "private-data"
    neighbor.write_text("unapproved")
    tool = binaries / ("tool-" + uuid.uuid4().hex)
    tool.write_text("#!/bin/sh\ncat " + shlex.quote(str(resources / "data")) + "\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.defpath)
    assert run(project, tool.name)["exit_code"] != 0
    requests = []
    manager = ScopedAccess(
        root=project,
        settings=ShellSandboxSettings(backend="seatbelt"),
        enabled=True,
        policy=HostFilePolicy.capture(),
        approve=lambda request: requests.append(request) or ApprovalDecision(True),
        record=lambda *_: None,
    )
    cmd = shlex.join(
        [
            sys.executable,
            "-I",
            "-c",
            "import subprocess,pathlib; "
            f"assert subprocess.check_output([{tool.name!r}],text=True)=='runtime-data'; "
            f"p=pathlib.Path({str(neighbor)!r}); assert not __import__('os').access(p,4); "
            f"p=pathlib.Path({str(resources / 'data')!r}); assert not __import__('os').access(p,2); "
            "print('scoped-runtime-ok')",
        ]
    )
    grant = manager.prepare(
        {"read_folders": [str(resources)], "reason": "Read the requested tool's runtime data"},
        command=cmd,
        tool="shell_run",
    )
    grant.check()
    result = run(project, cmd, build_shell_runner_from_settings(grant.settings, project))
    assert result["exit_code"] == 0 and "scoped-runtime-ok" in result["stdout"], result
    assert len(requests) == 1
    assert run(project, tool.name)["exit_code"] != 0
    assert neighbor.read_text() == "unapproved"
