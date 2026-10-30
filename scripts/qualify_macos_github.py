"""Local, disposable Seatbelt Git transport qualification (never dispatches CI).

Run with an installed candidate's Python -I. Host gh only manages the private
fixture, deploy key, trusted host keys, and independent API verification.
All repository Git and hooks run through the public shell_run tool under Seatbelt.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from alysis_code.agent.errors import ApprovalDeclinedError
from alysis_code.agent.scoped_access import ScopedAccess
from alysis_code.host_file_policy import HostFilePolicy
from alysis_code.sandbox_runner import SeatbeltShellRunner, build_shell_runner_from_settings
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.surface import ApprovalDecision
from alysis_code.tools.shell import shell_run


def main():
    if sys.platform != "darwin" or len(sys.argv) not in (2, 3):
        raise SystemExit(
            "Usage: python -I qualify_macos_github.py NEW_OUTPUT [PRIVATE_ARCHIVED_FIXTURE]"
        )
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    root = out / "workspace"
    root.mkdir()
    gh = shutil.which("gh")
    if not gh:
        raise SystemExit("GitHub CLI required for disposable fixture lifecycle")
    report = {
        "python": sys.executable,
        "runner_module": __import__("alysis_code.sandbox_runner", fromlist=[""]).__file__,
        "approval_source": "deterministic authorized callback",
        "events": [],
    }
    repo, key_id, agent, creds = None, None, None, None
    validated = False

    def save():
        (out / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    def host(argv, *, data=None, env=None):
        completed = subprocess.run(
            list(map(str, argv)),
            input=data,
            capture_output=True,
            text=True,
            timeout=45,
            env=env,
            cwd=out,
        )
        if completed.returncode:
            # Credential-producing commands must never put captured output in exceptions.
            raise RuntimeError(
                "Fixture setup/API command failed (exit " + str(completed.returncode) + ")"
            )
        return completed.stdout

    def api(endpoint, method="GET", body=None):
        argv = [gh, "api", endpoint, "--method", method]
        if body is not None:
            argv += ["--input", "-"]
        raw = host(argv, data=json.dumps(body) if body is not None else None)
        return json.loads(raw) if raw.strip() else None

    def approve(request):
        report["events"].append(
            {
                "name": "admission",
                "authentication": request.authentication,
                "preview": json.loads(request.preview),
            }
        )
        return ApprovalDecision(True)

    manager = ScopedAccess(
        root=root,
        settings=ShellSandboxSettings(backend="seatbelt", unavailable_fallback="refuse"),
        enabled=True,
        policy=HostFilePolicy.capture(),
        approve=approve,
        record=lambda *_: None,
    )

    def run(name, command, *, permissions=None, cwd=None, expected=0):
        grant = manager.prepare(permissions, command=command, tool="shell_run", cwd=cwd)
        grant.check()
        runner = build_shell_runner_from_settings(grant.settings, root)
        assert isinstance(runner, SeatbeltShellRunner), "Seatbelt required"
        result = shell_run(root=root, cwd=cwd, cmd=command, timeout_s=45, runner=runner)
        report["events"].append(
            {
                "name": name,
                "command": command,
                "result": result,
                "backend": "seatbelt",
                "expected": expected,
            }
        )
        save()
        print(json.dumps({"name": name, "exit_code": result["exit_code"]}), flush=True)
        assert (
            result["exit_code"] == expected if expected is not None else result["exit_code"] != 0
        ), name
        return result

    q = shlex.quote
    try:
        if len(sys.argv) == 3:
            repo = sys.argv[2]
            meta = api("repos/" + repo)
            assert meta["private"] and meta["archived"]
            assert not api("repos/" + repo + "/actions/permissions")["enabled"]
            assert not api("repos/" + repo + "/keys")
            validated = True
            api("repos/" + repo, "PATCH", {"archived": False})
        else:
            meta = api(
                "user/repos",
                "POST",
                {
                    "name": "alysis-mac-git-qa-" + uuid.uuid4().hex[:12],
                    "private": True,
                    "description": "Disposable synthetic Seatbelt qualification; no production data",
                },
            )
            repo = meta["full_name"]
            assert meta["private"]
            validated = True
            api("repos/" + repo + "/actions/permissions", "PUT", {"enabled": False})
        report["repository"] = repo
        save()
        assert not api("repos/" + repo + "/actions/permissions")["enabled"]
        https = "https://github.com/" + repo + ".git"
        rewritten = "git@github.com:" + repo + ".git"
        remote = "ssh://git@ssh.github.com:443/" + repo + ".git"
        network = {"network": True}
        account = {"network": True, "github_auth": True}
        run("https_network_denied", "git ls-remote " + https, expected=None)
        run(
            "https_credentials_absent", "git ls-remote " + https, permissions=network, expected=None
        )
        run("https_login", "git ls-remote " + https, permissions=account)
        run("https_clone", "git clone -- " + https + " https-clone", permissions=account)
        run(
            "ssh_url_over_https_clone",
            "git clone -- " + rewritten + " rewritten-clone",
            permissions=account,
        )
        before = run("saved_ssh_remote", "git remote get-url origin", cwd="rewritten-clone")
        assert before["stdout"].strip() == rewritten
        # Temporary repository-scoped deploy key; never print its bytes.
        creds = Path(tempfile.mkdtemp(prefix="alysis-mac-git-qa-", dir=Path.home())).resolve()
        key, hosts, config, agent_config = (
            creds / name for name in ("key", "known_hosts", "config", "agent-config")
        )
        host(
            [
                "/usr/bin/ssh-keygen",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                key,
                "-C",
                "disposable-seatbelt-qa",
            ]
        )
        public = key.with_suffix(".pub").read_text().strip()
        key_id = api(
            "repos/" + repo + "/keys",
            "POST",
            {"title": "Temporary Seatbelt qualification", "key": public, "read_only": False},
        )["id"]
        hosts.write_text(
            "".join("[ssh.github.com]:443 " + value + "\n" for value in api("meta")["ssh_keys"])
        )
        defaults = (
            'Host *\n  BatchMode yes\n  IdentitiesOnly yes\n  StrictHostKeyChecking yes\n  ConnectTimeout 8\n  UserKnownHostsFile "'
            + str(hosts)
            + '"\n'
        )
        config.write_text(defaults + '  IdentityAgent none\n  IdentityFile "' + str(key) + '"\n')
        public_path = root / "deploy-key.pub"
        public_path.write_text(public + "\n")
        agent_config.write_text(defaults + '  IdentityFile "' + str(public_path) + '"\n')
        for path in (key, hosts, config, agent_config):
            path.chmod(0o600)
        sibling = creds / "unapproved-sibling"
        sibling.write_text("synthetic-secret-sibling")
        outside = out / "outside-canary"
        outside.write_text("outside-unchanged")
        file_grants = {"read_files": list(map(str, (key, hosts, config)))}
        ssh_grants = {**file_grants, "network": True}
        ssh = "/usr/bin/ssh -F " + q(str(config))
        ssh_git = "git -c " + q("core.sshCommand=" + ssh) + " "
        auth_command = ssh + " -p 443 -T git@ssh.github.com"
        run("ssh_credentials_absent", auth_command, permissions=network, expected=None)
        run("ssh_network_denied", auth_command, permissions=file_grants, expected=None)
        actual = run("actual_ssh_authentication", auth_command, permissions=ssh_grants, expected=1)
        assert "successfully authenticated" in actual["stderr"]
        run(
            "strict_host_key_denial",
            auth_command + " -o HostKeyAlias=untrusted-fixture",
            permissions=ssh_grants,
            expected=255,
        )
        run(
            "sibling_credential_denial",
            "cat " + q(str(sibling)),
            permissions=file_grants,
            expected=None,
        )
        digest = hashlib.sha256(key.read_bytes()).hexdigest()
        run(
            "key_write_denial",
            "printf forbidden > " + q(str(key)),
            permissions=file_grants,
            expected=None,
        )
        assert hashlib.sha256(key.read_bytes()).hexdigest() == digest
        run("outside_read_denial", "cat " + q(str(outside)), expected=None)
        run("outside_write_denial", "printf forbidden > " + q(str(outside)), expected=None)
        run("credential_grant_expiry", auth_command, permissions=network, expected=None)
        run("network_grant_expiry", auth_command, permissions=file_grants, expected=None)
        # A fresh agent contains only this disposable deploy key, never user keys.
        raw = host(["/usr/bin/ssh-agent", "-s"])
        agent = dict(
            line.split(";", 1)[0].split("=", 1)
            for line in raw.splitlines()
            if line.startswith(("SSH_AUTH_SOCK=", "SSH_AGENT_PID="))
        )
        host(["/usr/bin/ssh-add", str(key)], env={**os.environ, **agent})
        os.environ["SSH_AUTH_SOCK"] = str(Path(agent["SSH_AUTH_SOCK"]).resolve())
        agent_grants = {
            "read_files": [str(hosts), str(agent_config)],
            "ssh_agent": True,
            "network": True,
        }
        agent_command = "/usr/bin/ssh -F " + q(str(agent_config)) + " -p 443 -T git@ssh.github.com"
        actual = run(
            "actual_ssh_agent_authentication", agent_command, permissions=agent_grants, expected=1
        )
        assert "successfully authenticated" in actual["stderr"]
        run(
            "ssh_agent_expiry",
            agent_command,
            permissions={"read_files": [str(hosts), str(agent_config)], "network": True},
            expected=None,
        )
        branch_id = uuid.uuid4().hex[:10]
        for transport, prefix, grants, directory in (
            ("https", "git ", account, "https-clone"),
            ("ssh", ssh_git, ssh_grants, "ssh-clone"),
        ):
            if transport == "ssh":
                run(
                    "ssh_clone", prefix + "clone -- " + remote + " " + directory, permissions=grants
                )
            branch = "qa/" + transport + "-" + branch_id
            run(transport + "_branch", "git switch -c " + branch, cwd=directory)
            # All configuration, edits, hooks and Git stay contained. Require SSH
            # signing rather than overriding the user's signing preference off.
            settings = {
                "user.name": "Seatbelt QA",
                "user.email": "qa@example.invalid",
                "gpg.format": "ssh",
                "user.signingkey": str(public_path),
                "commit.gpgsign": "true",
            }
            for name, value in settings.items():
                run(
                    transport + "_config_" + name,
                    "git config " + q(name) + " " + q(value),
                    cwd=directory,
                )
            run(
                transport + "_edit",
                "printf '%s\\n' "
                + q("contained synthetic " + transport + " edit " + branch_id)
                + " > parity.txt",
                cwd=directory,
            )
            run(transport + "_stage", "git add parity.txt", cwd=directory)
            run(
                transport + "_signing_required",
                "git commit -m 'Signed Seatbelt fixture [skip ci]'",
                cwd=directory,
                expected=None,
            )
            run(
                transport + "_signed_commit",
                "git commit -m 'Signed Seatbelt fixture [skip ci]'",
                cwd=directory,
                permissions={"ssh_agent": True},
            )
            (root / directory / "allowed_signers").write_text("qa@example.invalid " + public + "\n")
            run(
                transport + "_verify_signature",
                "git -c gpg.ssh.allowedSignersFile=allowed_signers verify-commit HEAD",
                cwd=directory,
            )
            sha = run(transport + "_sha", "git rev-parse HEAD", cwd=directory)["stdout"].strip()
            # Hook tries an outside read and write; both must fail. No raw token
            # or secret fixture content is printed, even if containment fails.
            hook = (
                "#!/bin/sh\nif cat "
                + q(str(sibling))
                + " >/dev/null 2>&1; then exit 91; fi\nif (printf changed > "
                + q(str(outside))
                + ") 2>/dev/null; then exit 92; fi\nprintf HOOK_CONTAINED\\n\n"
            )
            run(
                transport + "_install_hook",
                "printf %s " + q(hook) + " > .git/hooks/pre-push && chmod +x .git/hooks/pre-push",
                cwd=directory,
                permissions={"git_metadata": True},
            )
            pushed = run(
                transport + "_push",
                prefix + "push -u origin " + branch,
                cwd=directory,
                permissions=grants,
            )
            assert "HOOK_CONTAINED" in pushed["stdout"] + pushed["stderr"]
            assert api("repos/" + repo + "/git/ref/heads/" + branch)["object"]["sha"] == sha
            report.setdefault("remote_verification", []).append(
                {"transport": transport, "branch": branch, "sha": sha}
            )
            content = "synthetic remote " + transport + " update\n"
            api(
                "repos/" + repo + "/contents/remote-update.txt",
                "PUT",
                {
                    "message": "Synthetic pull control [skip ci]",
                    "branch": branch,
                    "content": base64.b64encode(content.encode()).decode(),
                    **(
                        {
                            "sha": api(
                                "repos/" + repo + "/contents/remote-update.txt?ref=" + branch
                            )["sha"]
                        }
                        if (root / directory / "remote-update.txt").exists()
                        else {}
                    ),
                },
            )
            run(transport + "_fetch", prefix + "fetch origin", cwd=directory, permissions=grants)
            run(
                transport + "_pull",
                prefix + "pull --ff-only origin " + branch,
                cwd=directory,
                permissions=grants,
            )
            assert (root / directory / "remote-update.txt").read_text() == content
            report["remote_verification"][-1]["pulled_content_verified"] = True
        run(
            "https_auth_expiry",
            "git fetch origin",
            cwd="https-clone",
            permissions=network,
            expected=None,
        )
        run("https_network_expiry", "git fetch origin", cwd="https-clone", expected=None)
        manager.approve = lambda _: ApprovalDecision(False)
        try:
            run("must_not_execute", "git clone " + https + " refused", permissions=account)
            raise AssertionError("Approval refusal was ignored")
        except ApprovalDeclinedError:
            assert not (root / "refused").exists()
            report["approval_refusal_prevented_execution"] = True
        assert outside.read_text() == "outside-unchanged"
        report["completed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
        print(json.dumps({"error": report["error"]}), flush=True)
    finally:
        errors = []
        if agent:
            try:
                host(["/usr/bin/ssh-agent", "-k"], env={**os.environ, **agent})
            except Exception as exc:
                errors.append(str(exc))
        if key_id is not None:
            try:
                api("repos/" + repo + "/keys/" + str(key_id), "DELETE")
            except Exception as exc:
                errors.append("Deploy-key revocation: " + str(exc))
        if validated:
            try:
                api("repos/" + repo, "PATCH", {"archived": True})
                meta = api("repos/" + repo)
                report["cleanup"] = {
                    "private": meta["private"],
                    "archived": meta["archived"],
                    "deploy_keys": len(api("repos/" + repo + "/keys")),
                    "actions_enabled": api("repos/" + repo + "/actions/permissions")["enabled"],
                    "workflow_runs": api("repos/" + repo + "/actions/runs")["total_count"],
                }
                assert report["cleanup"] == {
                    "private": True,
                    "archived": True,
                    "deploy_keys": 0,
                    "actions_enabled": False,
                    "workflow_runs": 0,
                }
            except Exception as exc:
                errors.append("Fixture archive/verification: " + str(exc))
        if creds:
            for path in creds.iterdir():
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            creds.rmdir()
            report["credential_files_removed"] = not creds.exists()
        if errors:
            report["cleanup_errors"] = errors
            report["completed"] = False
        save()
    return 0 if report.get("completed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
