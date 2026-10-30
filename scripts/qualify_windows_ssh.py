"""Disposable private SSH qualification; no keys or account tokens in evidence."""

import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import alysis_code.windows_mxc as module
from alysis_code.agent.scoped_access import ScopedAccess
from alysis_code.host_file_policy import HostFilePolicy
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.surface import ApprovalDecision
from alysis_code.tools.shell import shell_run
from alysis_code.windows_mxc import WindowsMxcRunner, resolve_helper

REPO = sys.argv[2] if len(sys.argv) == 3 else ""
if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", REPO) or REPO.split("/")[-1] in {".", ".."}:
    raise SystemExit(
        "Usage: python qualify_windows_ssh.py NEW_OUTPUT_DIRECTORY OWNER/PRIVATE_ARCHIVED_FIXTURE"
    )
OUT = Path(sys.argv[1]).resolve()
OUT.mkdir(exist_ok=False)
ROOT = OUT / "workspace"
ROOT.mkdir()
GH = Path("C:/Program Files/GitHub CLI/gh.exe")
GIT = Path("C:/Program Files/Git/cmd/git.exe")
SSH = Path(os.environ["SystemRoot"]) / "System32/OpenSSH/ssh.exe"
CREDS = Path.home() / ".ssh" / ("alysis-sandbox-qa-" + uuid.uuid4().hex)
KEY = CREDS / "id_ed25519"
CONFIG = CREDS / "config"
HOSTS = CREDS / "known_hosts"
CANARY = CREDS / "unapproved-sibling.txt"
REPORT = dict(
    python=sys.executable,
    module=module.__file__,
    events=[],
    repository=REPO,
    helper_sha256=hashlib.sha256(resolve_helper().read_bytes()).hexdigest(),
)
key_id = None
validated = False


def save():
    (OUT / "result.json").write_text(json.dumps(REPORT, indent=2))


def host(argv, *, data=None, cwd=OUT):
    result = subprocess.run(
        list(map(str, argv)), input=data, capture_output=True, text=True, timeout=60, cwd=cwd
    )
    if result.returncode:
        raise RuntimeError("Fixture command failed: " + result.stderr[:1000])
    return result.stdout


def api(endpoint, method="GET", body=None):
    args = [GH, "api", endpoint, "--method", method]
    if body is not None:
        args += ["--input", "-"]
    raw = host(args, data=json.dumps(body) if body is not None else None)
    return json.loads(raw) if raw.strip() else None


def host_git(*args, cwd=OUT):
    return host(
        [GIT, "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", *args],
        cwd=cwd,
    )


def q(value):
    return "'" + str(value).replace("'", "''") + "'"


def approve(request):
    REPORT["events"].append(
        dict(
            name="admission",
            authentication=request.authentication,
            preview=json.loads(request.preview),
        )
    )
    return ApprovalDecision(True)


manager = ScopedAccess(
    root=ROOT,
    settings=ShellSandboxSettings(backend="windows-mxc"),
    enabled=True,
    policy=HostFilePolicy((), ()),
    approve=approve,
    record=lambda *_: None,
)


def run(name, command, *, online=False, files=True, cwd=None, expected=0, native=False):
    proposal = {"network": online}
    if files:
        proposal["read_files"] = list(map(str, [KEY, CONFIG, HOSTS]))
    grant = manager.prepare(proposal, command=command, tool="shell_run", cwd=cwd)
    grant.check()
    result = shell_run(
        root=ROOT, cwd=cwd, cmd=command, timeout_s=35, runner=WindowsMxcRunner(grant.settings)
    )
    REPORT["events"].append(dict(name=name, command=command, result=result, expected=expected))
    save()
    print(
        json.dumps(
            dict(name=name, exit_code=result["exit_code"], backend=result.get("execution_backend"))
        ),
        flush=True,
    )
    if native:
        assert result.get("execution_backend") in {
            "contained-native-git",
            "contained-native-git-readonly",
        }, result
    assert result["exit_code"] == expected if expected is not None else result["exit_code"] != 0, (
        name,
        result,
    )
    return result


try:
    meta = api("repos/" + REPO)
    assert (
        meta["private"]
        and meta["archived"]
        and not api("repos/" + REPO + "/actions/permissions")["enabled"]
    )
    assert not api("repos/" + REPO + "/keys")
    validated = True
    api("repos/" + REPO, "PATCH", {"archived": False})
    CREDS.mkdir()
    sid = host(["whoami", "/user", "/fo", "csv", "/nh"]).strip().split(",")[-1].strip('"')
    host(["icacls", CREDS, "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"])
    host(
        [
            SSH.with_name("ssh-keygen.exe"),
            "-t",
            "ed25519",
            "-N",
            "",
            "-f",
            KEY,
            "-C",
            "disposable-private-sandbox-qa",
        ]
    )
    key_id = api(
        "repos/" + REPO + "/keys",
        "POST",
        dict(
            title="Temporary sandbox SSH qualification",
            key=KEY.with_suffix(".pub").read_text().strip(),
            read_only=False,
        ),
    )["id"]
    HOSTS.write_text(
        "".join("[ssh.github.com]:443 " + value + "\n" for value in api("meta")["ssh_keys"])
    )
    CONFIG.write_text(
        'Host *\n  BatchMode yes\n  IdentitiesOnly yes\n  IdentityAgent none\n  StrictHostKeyChecking yes\n  ConnectTimeout 8\n  IdentityFile "'
        + KEY.as_posix()
        + '"\n  UserKnownHostsFile "'
        + HOSTS.as_posix()
        + '"\n'
    )
    CANARY.write_text("synthetic-unapproved-sibling")
    remote = "ssh://git@ssh.github.com:443/" + REPO + ".git"
    sshcmd = '"' + SSH.as_posix() + '" -F "' + CONFIG.as_posix() + '"'
    git = "git -c " + q("core.sshCommand=" + sshcmd) + " "
    run("openssh_startup", "ssh -V; exit $LASTEXITCODE", files=False)
    auth = "ssh -F " + q(CONFIG) + " -p 443 -T git@ssh.github.com; exit $LASTEXITCODE"
    run("ssh_without_file_grant", auth, online=True, files=False, expected=None)
    run("ssh_network_off", auth, expected=None)
    authenticated = run("ssh_real_authentication", auth, online=True, expected=1)
    assert "successfully authenticated" in authenticated["stderr"], authenticated
    run(
        "strict_unknown_host_denied",
        "ssh -F "
        + q(CONFIG)
        + " -o HostKeyAlias=untrusted-fixture -p 443 -T git@ssh.github.com; exit $LASTEXITCODE",
        online=True,
        expected=255,
    )
    denied = (
        "try { [IO.File]::ReadAllText("
        + q(CANARY)
        + ") | Out-Null; exit 91 } catch { Write-Output 'SIBLING_DENIED'; exit 0 }"
    )
    assert "SIBLING_DENIED" in run("sibling_credential_denied", denied)["stdout"]
    unchanged = hashlib.sha256(KEY.read_bytes()).hexdigest()
    write = (
        "try { [IO.File]::WriteAllText("
        + q(KEY)
        + ",'should-not-write'); exit 91 } catch { Write-Output 'KEY_READONLY'; exit 0 }"
    )
    assert "KEY_READONLY" in run("granted_key_is_readonly", write)["stdout"]
    assert hashlib.sha256(KEY.read_bytes()).hexdigest() == unchanged
    run("native_ssh_ls_remote", git + "ls-remote " + q(remote), online=True, native=True)
    run("native_ssh_clone", git + "clone -- " + q(remote) + " ssh-clone", online=True, native=True)
    clone = ROOT / "ssh-clone"
    for key, value in {
        "user.name": "Sandbox QA",
        "user.email": "qa@example.invalid",
        "commit.gpgsign": "false",
    }.items():
        host_git("config", key, value, cwd=clone)
    branch = "test/ssh-" + OUT.name
    run("branch", "git switch -c " + branch, cwd="ssh-clone", files=False, native=True)
    (clone / "ssh-test.txt").write_text("real SSH transfer from contained Git\n")
    run("stage", "git add ssh-test.txt", cwd="ssh-clone", files=False, native=True)
    run(
        "commit",
        "git commit -m 'Private SSH sandbox test [skip ci]'",
        cwd="ssh-clone",
        files=False,
        native=True,
    )
    sha = host_git("rev-parse", "HEAD", cwd=clone).strip()
    run(
        "native_ssh_push",
        git + "push -u origin " + branch,
        cwd="ssh-clone",
        online=True,
        native=True,
    )
    assert api("repos/" + REPO + "/git/ref/heads/" + branch)["object"]["sha"] == sha
    run(
        "credential_grant_expired",
        git + "fetch origin",
        cwd="ssh-clone",
        online=True,
        files=False,
        expected=None,
        native=True,
    )
    run("network_grant_expired", git + "fetch origin", cwd="ssh-clone", expected=None, native=True)
    seed = OUT / "host-seed"
    host_git("clone", "--branch", branch, "https://github.com/" + REPO + ".git", str(seed))
    for key, value in {
        "user.name": "Sandbox QA",
        "user.email": "qa@example.invalid",
        "commit.gpgsign": "false",
    }.items():
        host_git("config", key, value, cwd=seed)
    (seed / "remote-ssh-update.txt").write_text("synthetic SSH pull control\n")
    host_git("add", "remote-ssh-update.txt", cwd=seed)
    host_git("commit", "-m", "Synthetic SSH pull control [skip ci]", cwd=seed)
    host_git("push", "origin", branch, cwd=seed)
    run("native_ssh_fetch", git + "fetch origin", cwd="ssh-clone", online=True, native=True)
    run(
        "native_ssh_pull",
        git + "pull --ff-only origin " + branch,
        cwd="ssh-clone",
        online=True,
        native=True,
    )
    assert (clone / "remote-ssh-update.txt").read_text() == "synthetic SSH pull control\n"
    REPORT["completed"] = True
except Exception as exc:
    REPORT["error"] = type(exc).__name__ + ": " + str(exc)
    print(REPORT["error"], flush=True)
finally:
    cleanup_errors = []
    if key_id is not None:
        try:
            api("repos/" + REPO + "/keys/" + str(key_id), "DELETE")
        except Exception as exc:
            cleanup_errors.append("Deploy-key revocation failed: " + str(exc))
    if validated:
        try:
            api("repos/" + REPO, "PATCH", {"archived": True})
            final = api("repos/" + REPO)
            REPORT["cleanup"] = dict(
                private=final["private"],
                archived=final["archived"],
                deploy_keys=len(api("repos/" + REPO + "/keys")),
                actions_enabled=api("repos/" + REPO + "/actions/permissions")["enabled"],
                workflow_runs=api("repos/" + REPO + "/actions/runs")["total_count"],
            )
            assert REPORT["cleanup"] == dict(
                private=True, archived=True, deploy_keys=0, actions_enabled=False, workflow_runs=0
            )
        except Exception as exc:
            cleanup_errors.append("Fixture cleanup failed: " + str(exc))
    for name in ["id_ed25519", "id_ed25519.pub", "known_hosts", "config", "unapproved-sibling.txt"]:
        path = CREDS / name
        if path.is_file() and path.resolve().parent == CREDS:
            path.unlink()
    if CREDS.is_dir():
        CREDS.rmdir()
    REPORT["credential_files_removed"] = not CREDS.exists()
    if cleanup_errors:
        REPORT["cleanup_errors"] = cleanup_errors
        REPORT["completed"] = False
    save()
sys.exit(0 if REPORT.get("completed") else 1)
