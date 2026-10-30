"""Actual Seatbelt and worker execution. No mocked sandbox or model is required."""

from __future__ import annotations

import json
import shlex
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

import alysis_code
from alysis_code.agent.errors import AgentRuntimeError
from alysis_code.config import ConfigError
from alysis_code.contained_read_tools import ContainedReadTools
from alysis_code.project_folders import folder_scope
from alysis_code.sandbox_runner import SeatbeltShellRunner
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.tools.fs import StaleFileError, capture_file_precondition

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires the real macOS Seatbelt")


def python_command(source):
    return shlex.join([sys.executable, "-I", "-B", "-c", source])


def run(root, source, **settings):
    result = SeatbeltShellRunner(**settings).run(
        root=root, cwd=root, cmd=python_command(source), timeout_s=20
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


@pytest.fixture
def scope(tmp_path, monkeypatch):
    paths = {name: tmp_path / name for name in ("main", "extra", "sibling", "config", "data")}
    for path in paths.values():
        path.mkdir()
        (path / "canary.txt").write_text("synthetic")
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(paths["config"]))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(paths["data"]))
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", "[]")
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", "[]")
    return paths


def test_scoped_access_child_inheritance_environment_and_private_scratch(scope, monkeypatch):
    main, extra, sibling = (scope[k] for k in ("main", "extra", "sibling"))
    monkeypatch.setenv("ALYSIS_TEST_SECRET", "synthetic-never-forward")
    source = f"""
import json, os, subprocess, sys, tempfile
from pathlib import Path
result = {{}}
for name, directory in { {k: str(scope[k]) for k in ("main", "extra", "sibling", "config", "data")}!r}.items():
    try: result[name + '_read'] = Path(directory, 'canary.txt').read_text()
    except PermissionError: result[name + '_read'] = 'denied'
    try: Path(directory, 'new.txt').write_text('written'); result[name + '_write'] = 'allowed'
    except PermissionError: result[name + '_write'] = 'denied'
result['secret'] = os.environ.get('ALYSIS_TEST_SECRET')
result['scratch'] = tempfile.gettempdir()
Path(result['scratch'], 'scratch.txt').write_text('temporary')
child = subprocess.run([sys.executable, '-I', '-B', '-c', "from pathlib import Path; Path({str(sibling / "child.txt")!r}).write_text('escape')"], capture_output=True)
result['child_denied'] = child.returncode != 0
print(json.dumps(result))
"""
    with folder_scope(main, [str(extra)]):
        result = run(main, source)
    for name in ("main", "extra"):
        assert result[name + "_read"] == "synthetic"
        assert result[name + "_write"] == "allowed"
    for name in ("sibling", "config", "data"):
        assert result[name + "_read"] == result[name + "_write"] == "denied"
    assert result["secret"] is None and result["child_denied"]
    assert not Path(result["scratch"]).exists()
    assert not (sibling / "child.txt").exists()
    assert (
        run(
            main,
            f"import os,json; print(json.dumps(os.access({str(extra / 'canary.txt')!r},os.R_OK)))",
        )
        is False
    )


def test_real_contained_read_mutation_and_revocation(scope):
    main, extra = scope["main"], scope["extra"]
    worker = ContainedReadTools(ShellSandboxSettings(backend="seatbelt"), main)
    with folder_scope(main, [str(extra)]):
        assert worker.run("fs_list", {"root_path": str(extra)})["returned_count"] == 1
        target = str(extra / "new.txt")
        args = {
            "path": target,
            "content": "after",
            "precondition": asdict(capture_file_precondition(root=main, path=target)),
        }
        assert worker.run_write("fs_write_prepared", args)["created"]
        assert (extra / "new.txt").read_text() == "after"
        with pytest.raises(StaleFileError):
            worker.run_write("fs_write_prepared", args)
        worker.run_write(
            "fs_copy", {"source_path": target, "destination_path": str(extra / "copy.txt")}
        )
        worker.run_write(
            "fs_move",
            {"source_path": str(extra / "copy.txt"), "destination_path": str(extra / "moved.txt")},
        )
        assert (extra / "moved.txt").read_text() == "after"
    with pytest.raises(ConfigError):
        worker.run("fs_list", {"root_path": str(extra)})
    readonly = ContainedReadTools(
        ShellSandboxSettings(backend="seatbelt", filesystem_access="read-only"), main
    )
    with pytest.raises(ConfigError, match="read-only"):
        readonly.run_write("fs_mkdir", {"path": "blocked"})


@pytest.mark.parametrize("filesystem_access", ["workspace-write", "read-only"])
def test_background_command_uses_current_project_folders_and_readonly_policy(
    scope, filesystem_access
):
    from alysis_code.background_runner import SeatbeltBackgroundRunner

    main, extra = scope["main"], scope["extra"]
    runner = SeatbeltBackgroundRunner(filesystem_access=filesystem_access)
    source = f"""
import json
from pathlib import Path
result = {{}}
try: result['read'] = Path({str(extra / "canary.txt")!r}).read_text()
except PermissionError: result['read'] = 'denied'
try: Path({str(extra / "background.txt")!r}).write_text('written'); result['write'] = 'allowed'
except PermissionError: result['write'] = 'denied'
print(json.dumps(result))
"""

    def execute():
        spawn = runner.start(root=main, cwd=main, cmd=python_command(source))
        try:
            output, error = spawn.popen.communicate(timeout=15)
            assert spawn.popen.returncode == 0, error
            return json.loads(output)
        finally:
            spawn.cleanup()

    with folder_scope(main, [str(extra)]):
        assert execute() == {
            "read": "synthetic",
            "write": "allowed" if filesystem_access == "workspace-write" else "denied",
        }
    assert execute() == {"read": "denied", "write": "denied"}


@pytest.mark.parametrize("exists", [False, True])
def test_custom_memory_in_attached_folder_cannot_bypass_memory_scope(scope, monkeypatch, exists):
    from alysis_code.local_memory import MemoryStore

    main, extra = scope["main"], scope["extra"]
    home = extra / "private-memory"
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(home))
    store = MemoryStore()
    if exists:
        store.change({"action": "save", "content": "synthetic other-project preference"})
    source = f"""
import json, os
from pathlib import Path
home = Path({str(home)!r})
result = {{}}
for name, action in (
    ('read', lambda: (home / 'memory.sqlite3').read_bytes()),
    ('write', lambda: (home / 'memory.sqlite3').write_bytes(b'corrupt')),
    ('rename', lambda: home.rename(home.with_name('stolen'))),
    ('plant', lambda: home.mkdir()),
):
    try: action(); result[name] = 'allowed'
    except OSError: result[name] = 'denied'
print(json.dumps(result))
"""
    with folder_scope(main, [str(extra)]):
        assert run(main, source) == dict.fromkeys(("read", "write", "rename", "plant"), "denied")
        worker = ContainedReadTools(ShellSandboxSettings(backend="seatbelt"), main)
        if exists:
            with pytest.raises(
                (ConfigError, RuntimeError), match="private|protected|denied|permitted"
            ):
                worker.run("fs_list", {"root_path": str(home)})
        else:
            assert worker.run("fs_list", {"root_path": str(home)})["returned_count"] == 0
    assert home.exists() is exists
    if exists:
        assert store.snapshot()["entries"][0]["content"] == "synthetic other-project preference"


@pytest.fixture
def file_tools(scope, monkeypatch):
    from test_approval_review import _Surface

    from alysis_code.agent.tools_assembly import build_tools
    from alysis_code.config import AppConfig
    from alysis_code.session_store import SessionStore

    for key, value in {
        "ALYSIS_SHELL_SANDBOX_BACKEND": "seatbelt",
        "ALYSIS_SHELL_SANDBOX_MODE": "strict",
        "ALYSIS_SHELL_SANDBOX_FILESYSTEM_ACCESS": "workspace-write",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }.items():
        monkeypatch.setenv(key, value)
    surface = _Surface()
    store = SessionStore(
        enabled=False,
        sessions_dir=scope["data"],
        session_id="external-files",
        cwd=str(scope["main"]),
        repo_root=str(scope["main"]),
    )
    tools = build_tools(
        root=scope["main"],
        console=None,
        surface=surface,
        store=store,
        cfg=AppConfig(),
        mode="review",
        yes=False,
        shell_runner=SeatbeltShellRunner(),
        runtime_kind="interactive_chat",
        skills_enabled=False,
        verification_enabled=False,
    )
    try:
        yield tools, surface
    finally:
        store.close()


@pytest.mark.parametrize("alias", [False, True, "system"])
def test_external_file_read_edit_and_new_nested_output(scope, file_tools, alias):
    tools, surface = file_tools
    external = scope["extra"]
    if alias == "system":
        if not str(external).startswith("/private/var/"):
            pytest.skip("Temporary directory is outside macOS's /var alias")
        external = Path(str(external).removeprefix("/private"))
    elif alias:
        external = scope["main"].parent / "folder alias"
        external.symlink_to(scope["extra"], target_is_directory=True)
    target = str(external / "canary.txt")
    assert "synthetic" in tools["fs_read"].run({"path": target})["content"]
    tools["fs_edit"].run(
        {
            "path": target,
            "edits": [{"op": "replace_exact", "target": "synthetic", "replacement": "updated"}],
        }
    )
    output = external / "Νέα 共有" / "nested" / "result.txt"
    tools["fs_write"].run({"path": str(output), "content": "new output"})
    assert (scope["extra"] / "canary.txt").read_text() == "updated"
    assert output.read_text() == "new output"
    assert (scope["sibling"] / "canary.txt").read_text() == "synthetic"
    assert any(request.kind == "sandbox_access" for request in surface.requests)


def test_attached_copy_move_and_symlink_escape_require_scope(scope, file_tools):
    tools, surface = file_tools
    main, extra, sibling = (scope[key] for key in ("main", "extra", "sibling"))
    link = extra / "outside"
    link.symlink_to(sibling, target_is_directory=True)
    with folder_scope(main, [str(extra)]):
        tools["fs_copy"].run(
            {"source_path": "canary.txt", "destination_path": str(extra / "copied.txt")}
        )
        tools["fs_move"].run(
            {"source_path": str(extra / "copied.txt"), "destination_path": "returned.txt"}
        )
        assert (main / "returned.txt").read_text() == "synthetic"
        assert not (extra / "copied.txt").exists()
        assert not any(request.kind == "sandbox_access" for request in surface.requests)
        surface.allow = False
        from alysis_code.agent.errors import ApprovalDeclinedError

        with pytest.raises((ApprovalDeclinedError, ConfigError)):
            tools["fs_read"].run({"path": str(link / "canary.txt")})
    assert (sibling / "canary.txt").read_text() == "synthetic"


def test_external_alias_retargeted_during_approval_does_not_run(scope, file_tools):
    from alysis_code.surface import ApprovalDecision

    tools, surface = file_tools
    alias = scope["main"].parent / "changing-alias"
    alias.symlink_to(scope["extra"], target_is_directory=True)

    def approve(request):
        alias.unlink()
        alias.symlink_to(scope["sibling"], target_is_directory=True)
        return ApprovalDecision(True)

    surface.request_approval = approve
    with pytest.raises(AgentRuntimeError, match="changed during approval") as refused:
        tools["fs_write"].run({"path": str(alias / "canary.txt"), "content": "bad"})
    assert refused.value.result_payload["error_code"] == "scoped_file_access_refused"
    assert refused.value.result_payload["dispatch_state"] == "not_dispatched"
    for name in ("extra", "sibling"):
        assert (scope[name] / "canary.txt").read_text() == "synthetic"


@pytest.mark.parametrize("metadata", [".git", ".alysis", ".agents"])
def test_metadata_alias_cannot_be_rebased_as_an_ordinary_external_folder(
    scope, file_tools, metadata
):
    tools, _surface = file_tools
    alias = scope["main"] / metadata
    alias.symlink_to(scope["extra"], target_is_directory=True)
    with pytest.raises(Exception, match="metadata|protected|Blocked"):
        tools["fs_write"].run({"path": str(alias / "new.txt"), "content": "bad"})
    assert not (scope["extra"] / "new.txt").exists()


@pytest.mark.parametrize("collision", ["file", "directory", "symlink"])
def test_mac_atomic_publication_never_replaces_existing_destination(scope, collision):
    source = scope["extra"] / "source.txt"
    destination = scope["main"] / "winner"
    source.write_text("source")
    if collision == "file":
        destination.write_text("winner")
    elif collision == "directory":
        destination.mkdir()
    else:
        destination.symlink_to(scope["sibling"] / "canary.txt")
    with folder_scope(scope["main"], [str(scope["extra"])]):
        result = run(
            scope["main"],
            f"""
import json
from pathlib import Path
from alysis_code.tools.fs import _publish_regular_file_no_replace
try:
    _publish_regular_file_no_replace(Path({str(source)!r}), Path({str(destination)!r}))
    result = 'replaced'
except FileExistsError:
    result = 'preserved'
print(json.dumps(result))
""",
            additional_read_roots=(str(Path(alysis_code.__file__).parent),),
        )
    assert result == "preserved"
    assert source.read_text() == "source"
    assert (scope["sibling"] / "canary.txt").read_text() == "synthetic"
    if collision == "file":
        assert destination.read_text() == "winner"
    elif collision == "directory":
        assert destination.is_dir()
    else:
        assert destination.is_symlink()


def test_directory_discovery_needs_no_content_grant_and_preserves_private_state(scope):
    main, extra, private = scope["main"], scope["extra"], scope["config"]
    nested = extra / "Δοκιμή website"
    nested.mkdir()
    (nested / ".git").mkdir()
    (extra / "private-alias").symlink_to(private, target_is_directory=True)
    source = f"""
import json, os
from pathlib import Path
result = {{}}
result['names'] = os.listdir({str(extra)!r})
result['repositories'] = [p for p, dirs, files in os.walk({str(extra)!r}) if '.git' in dirs]
for label, action in (
    ('content', lambda: Path({str(extra / "canary.txt")!r}).read_text()),
    ('write', lambda: Path({str(extra / "canary.txt")!r}).write_text('BAD')),
    ('private', lambda: os.listdir({str(private)!r})),
    ('alias', lambda: os.listdir({str(extra / "private-alias")!r})),
):
    try: action(); result[label] = 'allowed'
    except PermissionError: result[label] = 'denied'
print(json.dumps(result))
"""
    result = run(main, source)
    assert nested.name in result["names"]
    assert result["repositories"] == [str(nested)]
    assert all(result[key] == "denied" for key in ("content", "write", "private", "alias"))
    assert (extra / "canary.txt").read_text() == "synthetic"


@pytest.mark.parametrize("interrupt", ["cancel", "timeout"])
def test_preparation_can_stop_before_launch_and_cleans_scratch(scope, monkeypatch, interrupt):
    import os
    import subprocess
    import time

    import alysis_code.sandbox_runner as runner_module
    from alysis_code.process_reaping import CommandCancelledError

    root = scope["main"].resolve()
    original_scandir = os.scandir
    original_temporary_directory = runner_module.tempfile.TemporaryDirectory
    cancelled = False
    scratch = []

    def capture_scratch(*args, **kwargs):
        result = original_temporary_directory(*args, **kwargs)
        scratch.append(Path(result.name))
        return result

    def delayed_scandir(path):
        nonlocal cancelled
        if not isinstance(path, int) and Path(path) == root:
            if interrupt == "cancel":
                cancelled = True
            else:
                time.sleep(0.3)
        return original_scandir(path)

    monkeypatch.setattr(runner_module.tempfile, "TemporaryDirectory", capture_scratch)
    monkeypatch.setattr(os, "scandir", delayed_scandir)
    expected = CommandCancelledError if interrupt == "cancel" else subprocess.TimeoutExpired
    with pytest.raises(expected):
        SeatbeltShellRunner().run(
            root=root,
            cwd=root,
            cmd="echo launched > launched.txt",
            timeout_s=10 if interrupt == "cancel" else 0.2,
            cancel_check=lambda: cancelled,
        )
    assert not (root / "launched.txt").exists()
    assert scratch and all(not path.exists() for path in scratch)


@pytest.mark.parametrize("exit_code", [0, 9])
def test_external_shell_mutations_are_observed_even_on_failure(scope, file_tools, exit_code):
    from alysis_code.agent.verification import TurnExecutionState, _record_tool_effect

    tools, _surface = file_tools
    external = scope["extra"]
    target = external / "任意  όνομα.data"
    command = f"printf changed > {shlex.quote(target.name)}; exit {exit_code}"
    args = {
        "cmd": command,
        "cwd": str(external),
        "permissions": {"write_folders": [str(external)], "reason": "Edit the requested fixture"},
    }
    result = tools["shell_run"].run(args)
    assert result["exit_code"] == exit_code
    assert target.read_text() == "changed"
    assert result["touched_external_paths"] == [str(target)]
    assert not result.get("external_mutation_tracking_unavailable")
    state = TurnExecutionState(execution_requested=True)
    _record_tool_effect(
        root=scope["main"],
        state=state,
        tool_name="shell_run",
        arguments=args,
        status="ok",
        result=result,
        known_verification_commands=[],
    )
    assert state.touched_external_paths == {str(target)}
    assert state.verification_relevant_edit_generation == 1
    assert (scope["sibling"] / "canary.txt").read_text() == "synthetic"
    assert (scope["main"] / "canary.txt").read_text() == "synthetic"


def test_external_snapshot_does_not_follow_directory_or_file_aliases(scope):
    external = scope["extra"]
    (external / "private-link").symlink_to(scope["config"], target_is_directory=True)
    (external / "file-link").symlink_to(scope["config"] / "canary.txt")
    worker = ContainedReadTools(ShellSandboxSettings(backend="seatbelt"), external)
    snapshot = worker.run("mutation_snapshot", {})["signatures"]
    assert set(snapshot) == {"canary.txt", "private-link", "file-link"}
    assert all("synthetic" not in signature for signature in snapshot.values())
