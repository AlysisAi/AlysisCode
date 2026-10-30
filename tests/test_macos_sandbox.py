"""Actual Seatbelt and worker execution. No mocked sandbox or model is required."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from alysis_code.background_runner import SeatbeltBackgroundRunner
from alysis_code.config import ConfigError
from alysis_code.contained_read_tools import ContainedReadTools
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
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
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
    result = run(main, source, additional_workspace_roots=(str(extra),))
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


def test_readonly_shell_and_background_cannot_write_even_under_system_temp(scope):
    main = scope["main"]
    source = """import json, os
try: open('canary.txt','w').write('BAD'); result = 'written'
except PermissionError: result = 'denied'
print(json.dumps(result))"""
    assert run(main, source, filesystem_access="read-only") == "denied"
    spawn = SeatbeltBackgroundRunner(filesystem_access="read-only").start(
        root=main, cwd=main, cmd=python_command(source)
    )
    try:
        stdout, stderr = spawn.popen.communicate(timeout=20)
        assert spawn.popen.returncode == 0, stderr
        assert json.loads(stdout) == "denied"
    finally:
        if spawn.popen.poll() is None:
            spawn.popen.kill()
            spawn.popen.wait()
        spawn.cleanup()
    assert (main / "canary.txt").read_text() == "synthetic"


def test_private_runtime_metadata_and_ancestor_rename_remain_protected(scope, monkeypatch):
    main = scope["main"]
    private = main / "control" / "private"
    runtime = main / "runtime"
    metadata = main / ".git"
    for path in (private, runtime, metadata):
        path.mkdir(parents=True)
        (path / "canary.txt").write_text("synthetic")
    monkeypatch.setenv(
        "ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(private), str(main / "future-private")])
    )
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", json.dumps([str(runtime)]))
    (main / "alias").symlink_to(private, target_is_directory=True)
    source = f"""
import json, os
from pathlib import Path
result=[]
for path in {[str(p / "canary.txt") for p in (private, runtime, metadata)] + [str(main / "future-private")]!r}:
    try: Path(path).write_text('BAD'); result.append('written')
    except PermissionError: result.append('denied')
for path in {[str(private / "canary.txt"), str(main / "alias/canary.txt")]!r}:
    try: Path(path).read_text(); result.append('read')
    except PermissionError: result.append('denied')
for source,destination in {[(str(private.parent), str(main / "moved-control")), (str(runtime / "canary.txt"), str(main / "runtime-link"))]!r}:
    try:
        if Path(source).is_dir(): os.rename(source,destination)
        else: os.link(source,destination)
        result.append('changed')
    except PermissionError: result.append('denied')
print(json.dumps(result))
"""
    assert run(main, source) == ["denied"] * 8
    assert private.exists() and not (main / "runtime-link").exists()
    assert (runtime / "canary.txt").read_text() == "synthetic"


def test_existing_hardlink_cannot_alias_private_content(scope):
    alias = scope["main"] / "private-alias"
    alias.hardlink_to(scope["config"] / "canary.txt")
    with pytest.raises(ConfigError, match="hardlinked"):
        run(scope["main"], "print('unreachable')")


def test_internal_package_hardlinks_work_without_granting_external_aliases(scope):
    root = scope["main"]
    alias = root / "package-copy.txt"
    alias.hardlink_to(root / "canary.txt")
    assert (
        run(root, "import json;print(json.dumps(open('package-copy.txt').read()))") == "synthetic"
    )
    assert (
        run(
            root,
            "import json;print(json.dumps(open('package-copy.txt').read()))",
            filesystem_access="read-only",
        )
        == "synthetic"
    )
    external = scope["extra"] / "store-entry.txt"
    external.hardlink_to(alias)
    with pytest.raises(ConfigError, match="hardlinked"):
        run(root, "print('unreachable')")
    # A read-only store must never become writable through its project alias.
    with pytest.raises(ConfigError, match="hardlinked"):
        run(root, "print('unreachable')", additional_read_roots=(str(scope["extra"]),))
    assert (
        run(
            root,
            "import json;print(json.dumps(open('package-copy.txt').read()))",
            additional_workspace_roots=(str(scope["extra"]),),
        )
        == "synthetic"
    )


def test_git_command_list_short_circuit_and_metadata_boundary(scope):
    root = scope["main"]
    initialize_repository(root)
    (root / "canary.txt").write_text("chain change")
    runner = SeatbeltShellRunner()

    def command(text):
        return runner.run(root=root, cwd=root, cmd=text, timeout_s=20)

    result = command("git add . && git commit -m 'Chained commit' && git status --porcelain")
    assert result.returncode == 0, result.stderr
    assert git(root, "log", "-1", "--format=%s").stdout.strip() == "Chained commit"
    result = command("git switch missing-branch && git branch must-not-exist")
    assert result.returncode != 0
    assert "must-not-exist" not in git(root, "branch").stdout
    assert command("git switch missing-branch || git status").returncode == 0
    result = command("git status && printf BAD > .git/HEAD")
    assert result.returncode != 0
    assert git(root, "status").returncode == 0


def test_domain_proxy_blocks_other_domains_direct_traffic_and_closes(scope):
    import socket
    from urllib.parse import urlsplit

    from alysis_code.sandbox_runner import seatbelt_execution

    root = scope["main"]
    runner = SeatbeltShellRunner(network="on", network_domains=("registry.npmjs.org",))
    result = runner.run(
        root=root,
        cwd=root,
        cmd="curl --fail --silent --show-error --max-time 5 https://example.com/",
        timeout_s=10,
    )
    assert result.returncode != 0 and "403" in result.stderr
    source = """import socket,json
results=[]
for host in ('192.0.2.1','127.0.0.1'):
    with socket.socket() as s:
        try: s.connect((host,443));results.append('connected')
        except PermissionError:results.append('denied')
print(json.dumps(results))"""
    assert run(root, source, network="on", network_domains=("registry.npmjs.org",)) == [
        "denied",
        "denied",
    ]
    with seatbelt_execution(
        root=root,
        cwd=root,
        cmd="true",
        filesystem_access="workspace-write",
        network="on",
        clear_env=True,
        protect_repo_meta=True,
        safe_git_writes=True,
        additional_workspace_roots=(),
        network_domains=("registry.npmjs.org",),
    ) as (_, env, _):
        port = urlsplit(env["https_proxy"]).port
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.2)


def test_exact_unix_socket_access_does_not_grant_siblings_or_persist(scope):
    import socket
    import tempfile
    import threading

    # Darwin limits sockaddr_un paths to 104 bytes, including the terminator.
    with (
        tempfile.TemporaryDirectory(dir="/private/tmp", prefix="alysis-sockets-") as directory,
        socket.socket(socket.AF_UNIX) as server,
        socket.socket(socket.AF_UNIX) as sibling,
    ):
        path = Path(directory) / "daemon.sock"
        other = Path(directory) / "other.sock"
        server.bind(str(path))
        server.listen(1)
        server.settimeout(10)
        sibling.bind(str(other))
        sibling.listen(1)

        def serve():
            connection, _ = server.accept()
            with connection:
                connection.sendall(b"SOCKET_SCOPE_OK")

        worker = threading.Thread(target=serve)
        worker.start()
        source = f"""import socket,json
results=[]
for path in {[str(path), str(other)]!r}:
    with socket.socket(socket.AF_UNIX) as s:
        try:s.connect(path);results.append(s.recv(64).decode())
        except PermissionError:results.append('denied')
print(json.dumps(results))"""
        assert run(scope["main"], source, unix_sockets=(str(path),)) == [
            "SOCKET_SCOPE_OK",
            "denied",
        ]
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert run(scope["main"], source) == ["denied", "denied"]


def test_background_proxy_owns_lifetime_and_ignores_proxy_env_overrides(scope):
    import os
    import socket

    source = """import json,os
from urllib.parse import urlsplit
values={k:os.environ[k] for k in ('NO_PROXY','NODE_USE_ENV_PROXY','HOME','TMPDIR','PATH')}
values.update(proxy_port=urlsplit(os.environ['https_proxy']).port, proxies_match=os.environ['https_proxy']==os.environ['HTTPS_PROXY'])
print(json.dumps(values))"""
    root = scope["main"]
    spawn = SeatbeltBackgroundRunner(network="on", network_domains=("registry.npmjs.org",)).start(
        root=root,
        cwd=root,
        cmd=python_command(source),
        env_overrides={
            "HTTPS_PROXY": "http://unapproved.test:80",
            "https_proxy": "",
            "NO_PROXY": "*",
            "NODE_USE_ENV_PROXY": "0",
            "HOME": str(scope["sibling"]),
            "PATH": str(scope["sibling"]),
        },
    )
    try:
        stdout, stderr = spawn.popen.communicate(timeout=10)
        assert spawn.popen.returncode == 0, stderr
        values = json.loads(stdout)
        assert values["proxies_match"]
        assert values["NO_PROXY"] == "" and values["NODE_USE_ENV_PROXY"] == "1"
        assert values["HOME"] != str(scope["sibling"])
        assert values["PATH"] == os.environ.get("PATH", os.defpath)
        endpoint = ("127.0.0.1", values["proxy_port"])
        with socket.create_connection(endpoint, timeout=1) as client:
            client.sendall(b"CONNECT other.test:443 HTTP/1.1\r\nHost: other.test\r\n\r\n")
            assert client.recv(1024).startswith(b"HTTP/1.1 407")
    finally:
        if spawn.popen.poll() is None:
            spawn.popen.kill()
            spawn.popen.wait()
        spawn.cleanup()
    with pytest.raises(OSError):
        socket.create_connection(endpoint, timeout=0.2)
    assert not Path(values["TMPDIR"]).exists()


def test_scoped_install_reviews_effects_and_turn_folder_grant_expires(scope, monkeypatch):
    import io

    from rich.console import Console
    from test_approval_review import _reviewer, _Surface

    from alysis_code.agent.tools_assembly import build_tools
    from alysis_code.config import AppConfig

    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_BACKEND", "seatbelt")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "strict")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_FILESYSTEM_ACCESS", "workspace-write")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_NETWORK", "off")

    class Surface(_Surface):
        host_managed_approvals = True

    root = scope["main"]
    (root / "package.json").write_text('{"name":"scoped-test","private":true}')
    reviewer, client, store = _reviewer(
        root, [], user_message="Install dependencies and inspect the assets"
    )
    surface = Surface()
    tools = build_tools(
        root=root,
        cfg=AppConfig(),
        console=Console(file=io.StringIO()),
        surface=surface,
        store=store,
        mode="auto",
        yes=False,
        shell_runner=SeatbeltShellRunner(),
        approval_reviewer=reviewer,
        runtime_kind="interactive_chat",
        skills_enabled=False,
        subagents_enabled=False,
    )
    result = tools["shell_run"].run(
        {
            "cmd": "npm install --offline --ignore-scripts --no-audit --no-fund",
            "permissions": {"network_domains": ["registry.npmjs.org"]},
        }
    )
    assert result["exit_code"] == 0, result
    assert len(client.calls) == 1
    assert [request.kind for request in surface.requests] == ["shell_run"]
    assert "Unresolved command effects" in surface.requests[0].reason
    command = python_command(f"print(open({str(scope['extra'] / 'canary.txt')!r}).read())")
    assert (
        tools["shell_run"].run(
            {
                "cmd": command,
                "permissions": {"scope": "turn", "read_folders": [str(scope["extra"])]},
            }
        )["exit_code"]
        == 0
    )
    assert tools["shell_run"].run({"cmd": command})["exit_code"] == 0
    assert len(client.calls) == 2
    assert [request.kind for request in surface.requests] == ["shell_run"] * 3
    store.append("user_message", {"content": "Now only work inside the project"})
    assert tools["shell_run"].run({"cmd": command})["exit_code"] != 0


def test_inferred_toolchain_root_cannot_grant_the_whole_home(scope, monkeypatch):
    import alysis_code.macos_sandbox as macos_sandbox

    monkeypatch.setattr(
        macos_sandbox, "runtime_read_paths", lambda *_, **__: (Path.home().resolve(),)
    )
    with pytest.raises(ConfigError, match="home directory"):
        run(scope["main"], "print('unreachable')")


def test_contained_file_worker_reads_writes_and_rejects_stale_mutation(scope):
    root = scope["main"]
    worker = ContainedReadTools(ShellSandboxSettings(backend="seatbelt"), root)
    request = {
        "path": "canary.txt",
        "content": "changed",
        "precondition": asdict(capture_file_precondition(root=root, path="canary.txt")),
    }
    assert worker.run_write("fs_write_prepared", request)["bytes"] == 7
    assert (root / "canary.txt").read_text() == "changed"
    with pytest.raises(StaleFileError):
        worker.run_write("fs_write_prepared", request)
    assert "canary.txt" in json.dumps(worker.run("search_rg", {"pattern": "changed"}))
    readonly = ContainedReadTools(
        ShellSandboxSettings(backend="seatbelt", filesystem_access="read-only"), root
    )
    with pytest.raises(ConfigError, match="read-only"):
        readonly.run_write("fs_write_prepared", request)


def test_contained_search_does_not_read_parent_repository_metadata(scope):
    parent = scope["main"]
    # A linked parent repository has a .git file which ripgrep would open while
    # discovering Git ignore rules, even with --no-ignore-parent alone.
    (parent / ".git").write_text("gitdir: " + str(scope["sibling"]))
    root = parent / "nested-workspace"
    root.mkdir()
    (root / "visible.txt").write_text("scoped-match")
    (root / "ignored.txt").write_text("scoped-match")
    (root / ".gitignore").write_text("ignored.txt\n")
    worker = ContainedReadTools(ShellSandboxSettings(backend="seatbelt"), root)
    result = json.dumps(worker.run("search_rg", {"pattern": "scoped-match"}))
    assert "visible.txt" in result and "ignored.txt" not in result


def git(root, *arguments, **settings):
    return SeatbeltShellRunner(**settings).run(
        root=root, cwd=root, cmd=shlex.join(["git", *arguments]), timeout_s=20
    )


def initialize_repository(root):
    for arguments in [
        ("init", "-b", "main"),
        ("config", "user.name", "Seatbelt Fixture"),
        ("config", "user.email", "fixture@example.invalid"),
        ("add", "."),
        ("commit", "-m", "Initial"),
    ]:
        result = git(root, *arguments)
        assert result.returncode == 0, result.stderr


def test_real_git_branch_commit_hook_and_scoped_metadata(scope):
    root = scope["main"]
    initialize_repository(root)
    assert git(root, "switch", "-c", "δοκιμή").returncode == 0
    (root / "canary.txt").write_text("next\n")
    assert git(root, "add", "canary.txt").returncode == 0
    subject = "Διόρθωση '$()'; 世界"
    result = git(root, "commit", "-m", subject)
    assert result.returncode == 0, result.stderr
    assert git(root, "log", "-1", "--format=%s").stdout.strip() == subject
    assert git(root, "status", "--porcelain").stdout == ""
    assert git(root, "switch", "missing-branch").returncode != 0
    assert git(root, "branch", "forbidden", safe_git_writes=False).returncode != 0
    assert git(root, "branch", "forbidden", filesystem_access="read-only").returncode != 0
    assert (
        run(
            root,
            """import json
try: open('.git/HEAD','w').write('BAD'); result='written'
except PermissionError: result='denied'
print(json.dumps(result))""",
        )
        == "denied"
    )
    # Hooks and aliases receive the same OS restrictions as Git itself.
    hook = root / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\ncat " + shlex.quote(str(scope["config"] / "canary.txt")) + "\n")
    hook.chmod(0o700)
    failed = git(root, "commit", "--allow-empty", "-m", "Must fail")
    assert failed.returncode != 0 and "synthetic" not in failed.stdout
    escaped = git(root, "-c", "alias.escape=!echo BAD > ../sibling/canary.txt", "escape")
    assert escaped.returncode != 0
    assert (scope["sibling"] / "canary.txt").read_text() == "synthetic"
    private = root / ".alysis"
    private.mkdir()
    assert git(root, "-c", "alias.escape=!echo BAD > .alysis/changed", "escape").returncode != 0


def test_git_local_push_and_fast_forward_pull(scope):
    root, remote = (scope[k] for k in ("main", "extra"))
    peer = scope["sibling"] / "peer"
    peer.mkdir()
    initialize_repository(root)
    subprocess.run(["/usr/bin/git", "init", "--bare", str(remote)], check=True, capture_output=True)
    settings = {"additional_workspace_roots": (str(remote),)}
    assert git(root, "remote", "add", "origin", str(remote)).returncode == 0
    result = git(root, "push", "-u", "origin", "main", **settings)
    assert result.returncode == 0, result.stderr
    for arguments in [
        ["init", "-b", "main"],
        ["config", "user.name", "Peer"],
        ["config", "user.email", "peer@example.invalid"],
        ["remote", "add", "origin", str(remote)],
        ["pull", "origin", "main"],
        ["commit", "--allow-empty", "-m", "Peer commit"],
        ["push", "origin", "main"],
    ]:
        subprocess.run(
            ["/usr/bin/git", "-C", str(peer), *arguments], check=True, capture_output=True
        )
    before = (root / "canary.txt").read_bytes()
    result = git(root, "pull", "--ff-only", **settings)
    assert result.returncode == 0, result.stderr
    assert git(root, "log", "-1", "--format=%s").stdout.strip() == "Peer commit"
    assert (root / "canary.txt").read_bytes() == before
    # Removing the remote's explicit folder permission must deny further writes.
    assert git(root, "push", "origin", "HEAD:other").returncode != 0


def test_global_git_identity_is_projected_without_credentials_hooks_or_includes(scope, monkeypatch):
    config = scope["config"] / "gitconfig"
    config.write_text(
        '[user]\nname = "Δοκιμή \\"quoted\\" χρήστης"\nemail = fixture@example.invalid\n'
        "[credential]\nhelper = synthetic-private-helper\n"
        "[core]\nhooksPath = /outside/hooks\n"
        "[include]\npath = /outside/config\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    root = scope["main"]
    assert git(root, "init", "-b", "main").returncode == 0
    assert git(root, "add", ".").returncode == 0
    result = git(root, "commit", "-m", "Identity")
    assert result.returncode == 0, result.stderr
    assert git(root, "log", "-1", "--format=%an").stdout.strip() == 'Δοκιμή "quoted" χρήστης'
    projected = git(root, "config", "--global", "--list")
    assert projected.returncode == 0
    assert "credential" not in projected.stdout and "hooks" not in projected.stdout
    assert "include" not in projected.stdout
    config.write_text(
        config.read_text() + "[commit]\ngpgsign = true\n[gpg]\nformat = ssh\n"
        "[user]\nsigningkey = /outside/private-key\n"
    )
    assert git(root, "commit", "--allow-empty", "-m", "Must be signed").returncode != 0
    assert git(root, "log", "-1", "--format=%s").stdout.strip() == "Identity"


def test_linked_worktree_requires_explicit_common_metadata_scope(scope):
    root = scope["main"]
    initialize_repository(root)
    worktree = scope["extra"] / "linked"
    subprocess.run(
        ["/usr/bin/git", "-C", str(root), "worktree", "add", "-b", "linked", str(worktree)],
        check=True,
        capture_output=True,
    )
    assert git(worktree, "status").returncode != 0
    settings = {"additional_workspace_roots": (str(root / ".git"),)}
    assert git(worktree, "status", **settings).returncode == 0
    (worktree / "canary.txt").write_text("linked change\n")
    assert git(worktree, "add", ".", **settings).returncode == 0
    result = git(worktree, "commit", "-m", "Linked commit", **settings)
    assert result.returncode == 0, result.stderr
    assert (root / "canary.txt").read_text() == "synthetic"
    assert (
        run(
            worktree,
            f"""import json
try: open({str(root / ".git/config")!r}, 'w').write('BAD'); result='written'
except PermissionError: result='denied'
print(json.dumps(result))""",
            **settings,
        )
        == "denied"
    )


def test_shell_tool_uses_native_git_and_preserves_failure_status(scope):
    from alysis_code.sandbox_runner import build_shell_runner_from_settings
    from alysis_code.tools.shell import shell_run

    root = scope["main"]
    initialize_repository(root)
    runner = build_shell_runner_from_settings(ShellSandboxSettings(backend="auto"), root)
    assert isinstance(runner, SeatbeltShellRunner)
    created = shell_run(
        root=root, cmd="git switch -c tool-branch", runner=runner, capture_pipeline_status=True
    )
    assert created["exit_code"] == 0, created
    failed = shell_run(
        root=root, cmd="git switch missing-branch", runner=runner, capture_pipeline_status=True
    )
    assert failed["exit_code"] != 0
    assert failed["pipeline_stage_status"] == [failed["exit_code"]]
    # Shell compound syntax never obtains the direct Git invocation's grants.
    blocked = shell_run(root=root, cmd="git status; echo BAD > .git/HEAD", runner=runner)
    assert blocked["exit_code"] != 0
    assert (root / ".git/HEAD").read_text().strip() == "ref: refs/heads/tool-branch"


def test_cancellation_removes_private_scratch(scope):
    from alysis_code.process_reaping import CommandCancelledError

    root = scope["main"]
    marker = root / "scratch-path.txt"
    source = (
        "import os,time; from pathlib import Path; "
        "Path('scratch-path.txt').write_text(os.environ['TMPDIR']); time.sleep(20)"
    )
    with pytest.raises(CommandCancelledError):
        SeatbeltShellRunner().run(
            root=root,
            cwd=root,
            cmd=python_command(source),
            timeout_s=10,
            cancel_check=marker.exists,
        )
    assert not Path(marker.read_text()).exists()


def test_node_discovery_and_failure_are_real_exit_statuses(scope):
    import shutil

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed")
    root = scope["main"]
    (root / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}}))
    test = root / "example.test.cjs"
    test.write_text(
        "const test=require('node:test'); const assert=require('node:assert');\n"
        "test('real pass',()=>assert.equal(1,1));\n"
    )
    runner = SeatbeltShellRunner()
    discovery = ContainedReadTools(ShellSandboxSettings(backend="seatbelt"), root).run(
        "test_discover", {}
    )
    assert "npm test" in json.dumps(discovery), discovery

    def check():
        return runner.run(root=root, cwd=root, cmd=shlex.join([node, "--test"]), timeout_s=20)

    passed = check()
    assert passed.returncode == 0 and "real pass" in passed.stdout, passed.stderr
    test.write_text(test.read_text().replace("equal(1,1)", "equal(1,2)"))
    before = test.read_bytes()
    failed = check()
    assert failed.returncode != 0 and "real pass" in failed.stdout
    assert test.read_bytes() == before


def test_native_doctor_smoke_reports_seatbelt_without_docker(scope, monkeypatch):
    import alysis_code.sandbox_doctor as doctor
    from alysis_code.config import AppConfig
    from alysis_code.sandbox_doctor import diagnose_sandbox

    monkeypatch.setattr(
        doctor, "_run_docker_info", lambda: pytest.fail("Seatbelt must not depend on Docker")
    )
    cfg = AppConfig(model="offline-test")
    cfg.extra_fields = {"shell_sandbox": {"mode": "strict", "backend": "auto"}}
    result = diagnose_sandbox(cfg, include_smoke=True)
    assert result.ready and result.selected_backend == "seatbelt"
    assert any(
        check.name == "sandbox smoke test" and check.status == "ok" for check in result.checks
    )


def test_reviewed_read_and_write_grants_are_operation_local(scope):
    from alysis_code.agent.scoped_access import ScopedAccess
    from alysis_code.host_file_policy import HostFilePolicy
    from alysis_code.sandbox_runner import build_shell_runner_from_settings
    from alysis_code.surface import ApprovalDecision

    requests = []
    manager = ScopedAccess(
        root=scope["main"],
        settings=ShellSandboxSettings(backend="seatbelt"),
        approve=lambda request: requests.append(request) or ApprovalDecision(True),
        enabled=True,
        policy=HostFilePolicy.capture(),
        record=lambda *_: None,
    )
    command = python_command(
        "from pathlib import Path; print(Path('canary.txt').read_text()); Path('new.txt').write_text('written')"
    )
    grant = manager.prepare(
        {"read_folders": [str(scope["extra"])]}, command=command, tool="shell_run"
    )
    result = build_shell_runner_from_settings(grant.settings, scope["main"]).run(
        root=scope["main"], cwd=scope["extra"], cmd=command, timeout_s=10
    )
    assert result.stdout.strip() == "synthetic" and result.returncode != 0
    assert not (scope["extra"] / "new.txt").exists()
    grant = manager.prepare(
        {"write_folders": [str(scope["extra"])]}, command=command, tool="shell_run"
    )
    result = build_shell_runner_from_settings(grant.settings, scope["main"]).run(
        root=scope["main"], cwd=scope["extra"], cmd=command, timeout_s=10
    )
    assert result.returncode == 0 and (scope["extra"] / "new.txt").read_text() == "written"
    assert all(json.loads(req.preview)["sandbox"] == "strict seatbelt" for req in requests)
    assert manager.prepare(None, command=command, tool="shell_run").settings == manager.settings
    assert (
        run(
            scope["main"],
            f"import os,json;print(json.dumps(os.access({str(scope['extra'] / 'canary.txt')!r},os.R_OK)))",
        )
        is False
    )


def test_mac_tool_grant_crosses_review_and_does_not_survive_next_command(scope, monkeypatch):
    import io

    from rich.console import Console

    from alysis_code.agent.tools_assembly import build_tools
    from alysis_code.config import AppConfig
    from alysis_code.session_store import SessionStore
    from alysis_code.surface import ApprovalDecision
    from alysis_code.surface.noop_surface import NoopSurface

    class Surface(NoopSurface):
        host_managed_approvals = True

        def __init__(self):
            self.requests = []

        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision(True)

    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_BACKEND", "seatbelt")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "strict")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_FILESYSTEM_ACCESS", "workspace-write")
    root = scope["main"]
    surface = Surface()
    store = SessionStore(
        enabled=False,
        sessions_dir=scope["data"],
        session_id="scoped",
        cwd=str(root),
        repo_root=str(root),
    )
    tools = build_tools(
        root=root,
        cfg=AppConfig(),
        console=Console(file=io.StringIO()),
        surface=surface,
        store=store,
        mode="auto",
        yes=False,
        shell_runner=SeatbeltShellRunner(),
        runtime_kind="interactive_chat",
        skills_enabled=False,
        subagents_enabled=False,
    )
    assert "permissions" in tools["shell_run"].parameters["properties"]
    command = python_command(
        f"from pathlib import Path;print(Path({str(scope['extra'] / 'canary.txt')!r}).read_text())"
    )
    result = tools["shell_run"].run(
        {"cmd": command, "permissions": {"read_folders": [str(scope["extra"])]}}
    )
    assert result["exit_code"] == 0 and result["stdout"].strip() == "synthetic"
    assert [request.kind for request in surface.requests] == ["sandbox_access", "shell_run"]
    assert tools["shell_run"].run({"cmd": command})["exit_code"] != 0
    assert [request.kind for request in surface.requests] == [
        "sandbox_access",
        "shell_run",
        "shell_run",
    ]
    # External file tools use the same approval path and contained worker.
    read = tools["fs_read"].run({"path": str(scope["extra"] / "canary.txt")})
    assert "synthetic" in json.dumps(read)
    assert [request.kind for request in surface.requests] == [
        "sandbox_access",
        "shell_run",
        "shell_run",
        "sandbox_access",
    ]


@pytest.mark.parametrize("network", ["off", "on"])
def test_unix_sockets_cannot_escape_workspace_even_through_symlink(scope, monkeypatch, network):
    import socket

    root = scope["main"]
    private = root / "private-control"
    private.mkdir()
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(private)]))
    monkeypatch.chdir(root.parent)
    with (
        socket.socket(socket.AF_UNIX) as outside,
        socket.socket(socket.AF_UNIX) as nested,
        socket.socket(socket.AF_UNIX) as owned,
    ):
        paths = [scope["sibling"] / "outside.sock", private / "private.sock", root / "owned.sock"]
        for server, path in zip((outside, nested, owned), paths, strict=True):
            server.bind(str(path.relative_to(root.parent)))
            server.listen(3)
        (root / "alias.sock").symlink_to(paths[0])
        source = f"""import socket,json
results=[]
for path in {["../sibling/outside.sock", "private-control/private.sock", "owned.sock", "alias.sock"]!r}:
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        try: client.connect(path); results.append('connected')
        except PermissionError: results.append('denied')
print(json.dumps(results))"""
        assert run(root, source, network=network) == ["denied", "denied", "connected", "denied"]


def test_loopback_requires_local_preview_and_grant_does_not_allow_external_sockets(scope):
    import socket

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        port = server.getsockname()[1]
        source = f"""import socket,json
with socket.socket() as client:
    client.settimeout(2)
    try: client.connect(('127.0.0.1',{port})); result='connected'
    except PermissionError: result='denied'
print(json.dumps(result))"""
        for network in ("off", "on"):
            assert run(scope["main"], source, network=network) == "denied"
            assert (
                run(scope["main"], source, network=network, preview_access="local") == "connected"
            )
    source = """import socket,json
with socket.socket() as server:
    try: server.bind(('127.0.0.1',0)); server.listen(); result='listening'
    except PermissionError: result='denied'
print(json.dumps(result))"""
    assert run(scope["main"], source) == "denied"
    assert run(scope["main"], source, preview_access="local") == "listening"
    spawn = SeatbeltBackgroundRunner(preview_access="local").start(
        root=scope["main"], cwd=scope["main"], cmd=python_command(source)
    )
    try:
        stdout, stderr = spawn.popen.communicate(timeout=10)
        assert spawn.popen.returncode == 0, stderr
        assert json.loads(stdout) == "listening"
    finally:
        if spawn.popen.poll() is None:
            spawn.popen.kill()
            spawn.popen.wait()
        spawn.cleanup()
