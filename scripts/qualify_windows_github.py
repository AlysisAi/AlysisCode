"""Private synthetic GitHub test, with every Git observation using public shell_run.

Host gh is used only for fixture lifecycle/verification and approved login handoff.
No token values, request environments or private credentials are written here.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import alysis_code.git_native as module
from alysis_code.agent.scoped_access import ScopedAccess
from alysis_code.host_file_policy import HostFilePolicy
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.surface import ApprovalDecision
from alysis_code.tools.shell import shell_run
from alysis_code.windows_mxc import WindowsMxcRunner, resolve_helper

REPO = sys.argv[2] if len(sys.argv) == 3 else ""
if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", REPO) or REPO.split("/")[-1] in {".", ".."}:
    raise SystemExit(
        "Usage: python qualify_windows_github.py NEW_OUTPUT_DIRECTORY OWNER/PRIVATE_ARCHIVED_FIXTURE_REPO"
    )
OUTPUT = Path(sys.argv[1]).resolve()
OUTPUT.mkdir(exist_ok=False)
ROOT = OUTPUT / "workspace"
ROOT.mkdir()
GH = Path("C:/Program Files/GitHub CLI/gh.exe")
HOST_GIT = Path("C:/Program Files/Git/cmd/git.exe")
REPORT = {"python": sys.executable, "module": module.__file__, "repository": REPO, "events": []}
validated = False


def save():
    (OUTPUT / "result.json").write_text(json.dumps(REPORT, indent=2), encoding="utf-8")


def host(args, *, cwd=OUTPUT, data=None):
    result = subprocess.run(
        list(map(str, args)), cwd=cwd, input=data, capture_output=True, text=True, timeout=60
    )
    if result.returncode:
        raise RuntimeError("Host fixture command failed: " + result.stderr[:1000])
    return result.stdout


def api(endpoint, *, method="GET", body=None):
    args = [GH, "api", endpoint, "--method", method]
    if body is not None:
        args += ["--input", "-"]
    raw = host(args, data=json.dumps(body) if body else None)
    return json.loads(raw) if raw.strip() else None


def host_git(*args, cwd=OUTPUT):
    return host(
        [
            HOST_GIT,
            "-c",
            "credential.helper=",
            "-c",
            "credential.helper=!gh auth git-credential",
            *args,
        ],
        cwd=cwd,
    )


def approve(request):
    REPORT["events"].append(
        {
            "name": "admission",
            "command": request.command,
            "authentication": request.authentication,
            "preview": json.loads(request.preview),
        }
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


def git(name, command, *, cwd=None, online=False, auth=False, succeeds=True):
    grant = manager.prepare(
        {"network": online, "github_auth": auth}, command=command, tool="shell_run", cwd=cwd
    )
    grant.check()
    result = shell_run(
        root=ROOT, cwd=cwd, cmd=command, timeout_s=60, runner=WindowsMxcRunner(grant.settings)
    )
    assert result.get("execution_backend") in {
        "contained-native-git",
        "contained-native-git-readonly",
    }, result
    event = {"name": name, "command": command, "result": result, "expected_success": succeeds}
    REPORT["events"].append(event)
    save()
    print(
        json.dumps(
            {
                "name": name,
                "exit_code": result["exit_code"],
                "backend": result.get("execution_backend"),
            }
        ),
        flush=True,
    )
    assert (result["exit_code"] == 0) == succeeds, name + ": " + result.get("stderr", "")
    return result


try:
    import hashlib

    REPORT["helper_sha256"] = hashlib.sha256(resolve_helper().read_bytes()).hexdigest()
    meta = api("repos/" + REPO)
    assert meta["private"] and meta["archived"]
    assert not api("repos/" + REPO + "/actions/permissions")["enabled"]
    validated = True
    api("repos/" + REPO, method="PATCH", body={"archived": False})
    https = "https://github.com/" + REPO + ".git"
    ssh = "git@github.com:" + REPO + ".git"
    git("offline_remote_denied", f"git ls-remote {https}", succeeds=False)
    git("private_remote_without_auth_denied", f"git ls-remote {https}", online=True, succeeds=False)
    git("approved_private_https_read", f"git ls-remote {https}", online=True, auth=True)
    git("private_https_clone", f"git clone -- {https} https-clone", online=True, auth=True)
    git("github_ssh_url_uses_https", f"git clone -- {ssh} ssh-url-clone", online=True, auth=True)
    clone = ROOT / "https-clone"
    for key, value in {
        "user.name": "Sandbox QA",
        "user.email": "qa@example.invalid",
        "commit.gpgsign": "false",
    }.items():
        host_git("config", key, value, cwd=clone)
    branch = "test/" + OUTPUT.name
    git("branch", "git switch -c " + branch, cwd="https-clone")
    (clone / "counter.txt").write_text("contained native edit\n")
    git("stage", "git add counter.txt", cwd="https-clone")
    git("commit", "git commit -m 'Private sandbox live test [skip ci]'", cwd="https-clone")
    sha = host_git("rev-parse", "HEAD", cwd=clone).strip()
    git(
        "private_https_push",
        "git push -u origin " + branch,
        cwd="https-clone",
        online=True,
        auth=True,
    )
    assert api("repos/" + REPO + "/git/ref/heads/" + branch)["object"]["sha"] == sha
    git(
        "credential_grant_expired",
        "git fetch origin",
        cwd="https-clone",
        online=True,
        succeeds=False,
    )
    git("network_grant_expired", "git fetch origin", cwd="https-clone", succeeds=False)
    # A host-authored fixture update proves fetch/pull actually transfers new data.
    seed = OUTPUT / "host-seed"
    host_git("clone", "--branch", branch, https, str(seed))
    for key, value in {
        "user.name": "Sandbox QA",
        "user.email": "qa@example.invalid",
        "commit.gpgsign": "false",
    }.items():
        host_git("config", key, value, cwd=seed)
    (seed / "remote-update.txt").write_text("synthetic remote fixture update\n")
    host_git("add", "remote-update.txt", cwd=seed)
    host_git("commit", "-m", "Synthetic pull control [skip ci]", cwd=seed)
    host_git("push", "origin", branch, cwd=seed)
    git("private_fetch", "git fetch origin", cwd="https-clone", online=True, auth=True)
    git(
        "private_pull",
        "git pull --ff-only origin " + branch,
        cwd="https-clone",
        online=True,
        auth=True,
    )
    assert (clone / "remote-update.txt").read_text() == "synthetic remote fixture update\n"
    # Rewrites are operation-local; the origin URL on disk is unchanged.
    assert host_git("remote", "get-url", "origin", cwd=ROOT / "ssh-url-clone").strip() == ssh
    git("github_ssh_url_fetch", "git fetch origin", cwd="ssh-url-clone", online=True, auth=True)
    assert not manager.prepare(
        None, command="git fetch origin", tool="shell_run"
    ).settings.github_auth
    REPORT["completed"] = True
except Exception as exc:
    REPORT["error"] = type(exc).__name__ + ": " + str(exc)
    print(REPORT["error"], flush=True)
finally:
    if not validated:
        save()
        raise SystemExit(1)
    try:
        api("repos/" + REPO, method="PATCH", body={"archived": True})
        final = api("repos/" + REPO)
        REPORT["cleanup"] = {
            "private": final["private"],
            "archived": final["archived"],
            "deploy_keys": len(api("repos/" + REPO + "/keys")),
            "actions_enabled": api("repos/" + REPO + "/actions/permissions")["enabled"],
            "workflow_runs": api("repos/" + REPO + "/actions/runs")["total_count"],
        }
        assert REPORT["cleanup"] == {
            "private": True,
            "archived": True,
            "deploy_keys": 0,
            "actions_enabled": False,
            "workflow_runs": 0,
        }
    except Exception as exc:
        REPORT["cleanup_error"] = str(exc)
    save()
raise SystemExit(0 if REPORT.get("completed") and not REPORT.get("cleanup_error") else 1)
