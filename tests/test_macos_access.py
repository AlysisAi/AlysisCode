"""Real Seatbelt authentication inputs, without using or writing user credentials."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from alysis_code.agent.errors import ApprovalDeclinedError
from alysis_code.agent.scoped_access import ScopedAccess
from alysis_code.background_runner import SeatbeltBackgroundRunner
from alysis_code.config import ConfigError
from alysis_code.host_file_policy import HostFilePolicy, HostFilePolicyError
from alysis_code.macos_access import environment_names, file_identity
from alysis_code.sandbox_runner import build_shell_runner_from_settings
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.surface import ApprovalDecision

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="actual macOS Seatbelt")


def command(source):
    return shlex.join([sys.executable, "-I", "-B", "-c", source])


@pytest.fixture
def auth(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "private-config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "private-data"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", "[]")
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", "[]")
    requests, events = [], []
    manager = ScopedAccess(
        root=root,
        settings=ShellSandboxSettings(backend="seatbelt"),
        enabled=True,
        policy=HostFilePolicy.capture(),
        approve=lambda r: requests.append(r) or ApprovalDecision(True),
        record=lambda *e: events.append(e),
    )
    return manager, requests, events


def execute(manager, proposal, cmd):
    grant = manager.prepare(proposal, command=cmd, tool="shell_run")
    grant.check()
    runner = build_shell_runner_from_settings(grant.settings, manager.root)
    return runner.run(root=manager.root, cwd=manager.root, cmd=cmd, timeout_s=20)


def test_exact_configuration_file_read_with_no_sibling_or_write_access(auth, tmp_path):
    manager, requests, events = auth
    config = tmp_path / "tool-config"
    config.write_text("placeholder-config")
    sibling = tmp_path / "other-config"
    sibling.write_text("placeholder-other")
    source = f"""import json
from pathlib import Path
result = {{'config': Path({str(config)!r}).read_text() == 'placeholder-config'}}
for kind, path in [('write', {str(config)!r}), ('sibling', {str(sibling)!r})]:
    try:
        if kind == 'write': Path(path).write_text('changed')
        else: Path(path).read_text()
        result[kind] = 'allowed'
    except PermissionError: result[kind] = 'denied'
print(json.dumps(result))"""
    result = execute(manager, {"read_files": [str(config)]}, command(source))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"config": True, "write": "denied", "sibling": "denied"}
    assert requests[0].authentication
    assert requests[0].authentication_files == (file_identity(str(config)),)
    assert requests[0].metadata.get("mandatory_explicit_approval") is True
    assert "placeholder-config" not in requests[0].preview
    assert execute(manager, None, command(f"open({str(config)!r}).read()")).returncode != 0
    assert config.read_text() == "placeholder-config"


def test_person_approved_authentication_stays_exact_and_expires(auth, tmp_path, monkeypatch):
    from test_approval_review import _reviewer, _Surface

    from alysis_code.agent.action_approval import decide_action_approval

    manager, requests, _ = auth
    config = tmp_path / "authentication-config"
    config.write_text("synthetic-input")
    monkeypatch.setenv("APPROVAL_TEST_AUTH_TOKEN", "synthetic-input")
    reviewer, _, _ = _reviewer(tmp_path, [], user_message="Run the synthetic authentication check.")
    surface = _Surface()

    def approve(request):
        requests.append(request)
        return decide_action_approval(
            request,
            mode="auto",
            root=manager.root,
            surface=surface,
            can_ask_person=True,
            reviewer=reviewer,
        )

    manager.approve = approve
    cmd = command(
        f"import os; assert open({str(config)!r}).read() == os.environ['APPROVAL_TEST_AUTH_TOKEN']; print('AUTH_OK')"
    )
    result = execute(
        manager, {"read_files": [str(config)], "environment": ["APPROVAL_TEST_AUTH_TOKEN"]}, cmd
    )
    assert result.returncode == 0 and result.stdout.strip() == "AUTH_OK", result.stderr
    assert len(surface.requests) == 1
    assert surface.requests[0].metadata["always_ask_person"]
    assert requests[-1].metadata["allow_for_session_disabled"]
    assert execute(manager, None, cmd).returncode != 0


def test_environment_forwarding_is_exact_ephemeral_and_background_cannot_replace_it(
    auth, monkeypatch
):
    manager, requests, events = auth
    monkeypatch.setenv("REGISTRY_TOKEN", "placeholder-approved")
    monkeypatch.setenv("CLOUD_TOKEN", "placeholder-unapproved")
    cmd = command(
        "import os,json; print(json.dumps([os.getenv('REGISTRY_TOKEN') == 'placeholder-approved', 'CLOUD_TOKEN' in os.environ]))"
    )
    result = execute(manager, {"environment": ["REGISTRY_TOKEN"]}, cmd)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [True, False]
    assert all("placeholder-approved" not in str(item) for item in [requests[0].preview, events])
    assert json.loads(execute(manager, None, cmd).stdout) == [False, False]
    spawned = SeatbeltBackgroundRunner(forward_env=("REGISTRY_TOKEN",)).start(
        root=manager.root, cwd=manager.root, cmd=cmd, env_overrides={"REGISTRY_TOKEN": "override"}
    )
    try:
        out, err = spawned.popen.communicate(timeout=20)
        assert spawned.popen.returncode == 0, err
        assert json.loads(out) == [True, False]
    finally:
        if spawned.popen.poll() is None:
            spawned.popen.kill()
            spawned.popen.wait()
        spawned.cleanup()


@pytest.mark.parametrize(
    "name",
    [
        "PATH",
        "DYLD_INSERT_LIBRARIES",
        "NODE_OPTIONS",
        "HTTP_PROXY",
        "SSH_AUTH_SOCK",
        "GIT_CONFIG_GLOBAL",
        "ALYSIS_API_KEY",
    ],
)
def test_policy_environment_cannot_be_forwarded(monkeypatch, name):
    monkeypatch.setenv(name, "placeholder")
    with pytest.raises(ConfigError, match="policy"):
        environment_names([name])


def test_approval_decline_input_change_and_retention_are_rejected(auth, tmp_path, monkeypatch):
    manager, _, _ = auth
    config = tmp_path / "config"
    config.write_text("placeholder")
    monkeypatch.setenv("REGISTRY_TOKEN", "placeholder")
    for proposal in (
        {"read_files": [str(config)]},
        {"environment": ["REGISTRY_TOKEN"]},
        {"git_metadata": True},
    ):
        manager.events_since = lambda c: ([], c)
        with pytest.raises(ConfigError, match="one operation"):
            manager.prepare({**proposal, "scope": "turn"}, command="true", tool="shell_run")
    manager.approve = lambda _: ApprovalDecision(False)
    with pytest.raises(ApprovalDeclinedError):
        execute(manager, {"environment": ["REGISTRY_TOKEN"]}, "true")

    def change_env(_):
        monkeypatch.setenv("REGISTRY_TOKEN", "changed-placeholder")
        return ApprovalDecision(True)

    manager.approve = change_env
    with pytest.raises(ConfigError, match="environment changed"):
        execute(manager, {"environment": ["REGISTRY_TOKEN"]}, "true")

    def change_file(_):
        config.write_text("changed-placeholder")
        return ApprovalDecision(True)

    manager.approve = change_file
    with pytest.raises(ConfigError, match="file changed"):
        execute(manager, {"read_files": [str(config)]}, "true")
    private = Path(os.environ["ALYSIS_CONFIG_DIR"])
    private.mkdir()
    (private / "settings").write_text("placeholder")
    with pytest.raises(HostFilePolicyError):
        file_identity(str(private / "settings"))


def test_mixed_git_command_is_explicit_and_does_not_expose_application_metadata(auth):
    manager, _, _ = auth
    subprocess.run(["/usr/bin/git", "init", "-q", str(manager.root)], check=True)
    (manager.root / ".alysis").mkdir()
    (manager.root / ".alysis" / "settings").write_text("protected")
    source = "from pathlib import Path; Path('result.txt').write_text('built')"
    cmd = (
        command(source)
        + " && /usr/bin/git add result.txt && /usr/bin/git -c user.name=Test -c user.email=test@example.invalid -c commit.gpgsign=false commit -qm build"
    )
    assert execute(manager, None, cmd).returncode != 0
    approved = execute(manager, {"git_metadata": True}, cmd)
    assert approved.returncode == 0, approved.stderr
    protected = execute(
        manager,
        {"git_metadata": True},
        command("from pathlib import Path; Path('.alysis/settings').write_text('bad')"),
    )
    assert protected.returncode != 0
    assert (manager.root / ".alysis/settings").read_text() == "protected"
    assert (
        execute(
            manager,
            None,
            command("from pathlib import Path; Path('.git/canary').write_text('bad')"),
        ).returncode
        != 0
    )


def test_public_shell_reports_metadata_policy_and_honors_exact_script_approval(auth):
    from alysis_code.tools.shell import shell_run

    manager, requests, _ = auth

    def run(cmd, proposal=None, cwd=None):
        grant = manager.prepare(proposal, command=cmd, tool="shell_run")
        grant.check()
        runner = build_shell_runner_from_settings(grant.settings, manager.root)
        return shell_run(root=manager.root, cwd=cwd, cmd=cmd, runner=runner)

    assert run("git init -q clone")["exit_code"] == 0
    script = "cd clone && git checkout -b qa/nested && git config user.name 'QA'"
    for proposal in (None, {"write_folders": [str(manager.root)]}):
        result = run(script, proposal)
        assert result["exit_code"] != 0
        assert result["sandbox_policy"]["git_metadata"] == "protected"
        assert "permissions.git_metadata=true" in result["sandbox_policy"]["guidance"]
        assert not (manager.root / "clone/.git/config").read_text().endswith("name = QA\n")

    # The policy report is independent of stderr language or command classification.
    unrelated = run("printf 'ανεξάρτητο σφάλμα' >&2; exit 23")
    assert unrelated["exit_code"] == 23
    assert unrelated["stderr"] == "ανεξάρτητο σφάλμα"
    assert unrelated["sandbox_policy"]["git_metadata"] == "protected"

    manager.approve = lambda _: ApprovalDecision(False)
    with pytest.raises(ApprovalDeclinedError):
        run(script, {"git_metadata": True})
    assert "name = QA" not in (manager.root / "clone/.git/config").read_text()

    manager.approve = lambda request: requests.append(request) or ApprovalDecision(True)
    result = run(script, {"git_metadata": True})
    assert result["exit_code"] == 0, result
    assert "sandbox_policy" not in result
    assert json.loads(requests[-1].preview)["git_metadata_write"] is True
    assert "name = QA" in (manager.root / "clone/.git/config").read_text()
    assert run("cd clone && git config user.name Changed")["exit_code"] != 0
    assert "name = QA" in (manager.root / "clone/.git/config").read_text()
    assert run("git config user.name Changed", cwd="clone")["exit_code"] == 0
    assert "name = Changed" in (manager.root / "clone/.git/config").read_text()


def test_real_ssh_agent_signed_git_commit_without_private_key_files(auth, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    manager, requests, _ = auth
    # Agent chooses a short /tmp socket; a private key exists in memory only.
    launch = subprocess.run(
        ["/usr/bin/ssh-agent", "-s"], capture_output=True, text=True, check=True
    )
    values = dict(
        line.split(";", 1)[0].split("=", 1)
        for line in launch.stdout.splitlines()
        if line.startswith(("SSH_AUTH_SOCK=", "SSH_AGENT_PID="))
    )
    environment = {**os.environ, **values}
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()
    try:
        added = subprocess.run(
            ["/usr/bin/ssh-add", "-"],
            input=key.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()),
            capture_output=True,
            env=environment,
        )
        assert added.returncode == 0  # never include credential input in diagnostics
        monkeypatch.setenv("SSH_AUTH_SOCK", values["SSH_AUTH_SOCK"])
        subprocess.run(["/usr/bin/git", "init", "-q", str(manager.root)], check=True)
        (manager.root / "allowed_signers").write_text("test@example.invalid " + public + "\n")
        cmd = shlex.join(
            [
                "/usr/bin/git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "gpg.format=ssh",
                "-c",
                "user.signingkey=key::" + public,
                "commit",
                "--allow-empty",
                "-S",
                "-m",
                "signed fixture",
            ]
        )
        assert execute(manager, None, cmd).returncode != 0
        signed = execute(manager, {"ssh_agent": True}, cmd)
        assert signed.returncode == 0, signed.stderr
        verify = execute(
            manager,
            None,
            "/usr/bin/git -c gpg.ssh.allowedSignersFile=allowed_signers verify-commit HEAD",
        )
        assert verify.returncode == 0, verify.stderr
        assert requests[-1].authentication
        assert requests[-1].metadata["mandatory_explicit_approval"] is True
        assert requests[-1].metadata["always_ask_person"] is True
        assert requests[-1].metadata["allow_for_session_disabled"] is True
        assert execute(manager, None, cmd).returncode != 0
    finally:
        subprocess.run(
            ["/usr/bin/ssh-agent", "-k"], env=environment, capture_output=True, check=True
        )


def test_actual_private_http_endpoint_through_proxy_without_direct_socket_escape(auth):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    manager, requests, _ = auth

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"LOCAL_ENDPOINT_OK")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_port
        source = f"""import json, os, socket, urllib.request
proxy = urllib.request.ProxyHandler({{'http': os.environ['http_proxy']}})
body = urllib.request.build_opener(proxy).open('http://127.0.0.1:{port}/', timeout=4).read().decode()
try: socket.create_connection(('127.0.0.1', {port}), timeout=1); direct = 'allowed'
except PermissionError: direct = 'denied'
print(json.dumps([body, direct]))"""
        result = execute(
            manager, {"private_network_endpoints": [f"127.0.0.1:{port}"]}, command(source)
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == ["LOCAL_ENDPOINT_OK", "denied"]
        assert requests[-1].metadata["mandatory_explicit_approval"]
        denied = execute(
            manager,
            None,
            command(f"import socket; socket.create_connection(('127.0.0.1',{port}),timeout=1)"),
        )
        assert denied.returncode != 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "hosted,depth,one_shot", [(True, 1, False), (True, 0, True), (False, 0, True)]
)
def test_hosted_children_and_one_shot_use_existing_approval_authority(
    auth, hosted, depth, one_shot
):
    import io

    from rich.console import Console
    from test_approval_review import _reviewer, _Surface

    from alysis_code.agent.tools_assembly import build_tools
    from alysis_code.runtime_kind import RuntimeKind

    manager, _, _ = auth
    reviewer, client, store = _reviewer(manager.root, [])
    surface = _Surface()
    surface.host_managed_approvals = hosted
    tools = build_tools(
        root=manager.root,
        console=Console(file=io.StringIO()),
        surface=surface,
        store=store,
        mode="auto",
        yes=False,
        non_interactive=True,
        shell_runner=build_shell_runner_from_settings(manager.settings, manager.root),
        runtime_kind=RuntimeKind.SUBAGENT if depth else RuntimeKind.ONE_SHOT,
        subagent_depth=depth,
        one_shot_execution=one_shot,
        approval_reviewer=reviewer,
        verification_enabled=False,
    )
    assert ("permissions" in tools["shell_run"].parameters["properties"]) is hosted
    if hosted:
        result = tools["shell_run"].run(
            {
                "cmd": "echo HOSTED_SCOPE_OK",
                "permissions": {"network_domains": ["registry.npmjs.org"]},
            }
        )
        assert result["exit_code"] == 0 and "HOSTED_SCOPE_OK" in result["stdout"]
        assert len(client.calls) == 1 and not surface.requests
