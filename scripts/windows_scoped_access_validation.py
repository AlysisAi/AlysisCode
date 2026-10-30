"""Installed Windows MXC grant lifetimes; explicit synthetic host approvals, no model."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
from pathlib import Path

from rich.console import Console

from alysis_code.agent.sandbox_access_store import SandboxAccessStore
from alysis_code.agent.scoped_access import ScopedAccess
from alysis_code.agent.tools_assembly import build_tools
from alysis_code.config import AppConfig, ConfigError
from alysis_code.host_file_policy import HostFilePolicy
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.sandbox_runner import build_shell_runner_from_settings
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.session_store import SessionStore
from alysis_code.surface import ApprovalDecision, NoopSurface
from alysis_code.windows_mxc import resolve_helper


def main() -> None:
    output = Path(sys.argv[1]).resolve()
    output.mkdir(exist_ok=False)
    folders = {
        name: output / name for name in ("project", "assets", "sibling", "config", "data", "memory")
    }
    for folder in folders.values():
        folder.mkdir()
        (folder / "canary.txt").write_text(folder.name, encoding="utf-8")
    for key, name in (
        ("ALYSIS_CONFIG_DIR", "config"),
        ("ALYSIS_DATA_DIR", "data"),
        ("ALYSIS_MEMORY_HOME", "memory"),
    ):
        os.environ[key] = str(folders[name])
    node = Path(shutil.which("node")).resolve(strict=True)
    settings = ShellSandboxSettings(
        backend="windows-mxc",
        mode="strict",
        network="off",
        filesystem_access="workspace-write",
        windows_read_roots=(str(node.parent),),
    )
    report = {
        "passed": False,
        "python": sys.executable,
        "helperSha256": hashlib.sha256(resolve_helper().read_bytes()).hexdigest(),
        "approvalSource": "explicit synthetic host; real native processes and grant storage",
        "checks": [],
    }
    approvals, records, messages = [], [], []

    def manager(session="one"):
        return ScopedAccess(
            root=folders["project"],
            settings=settings,
            enabled=True,
            policy=HostFilePolicy((), ()),
            approve=lambda request: approvals.append(request) or ApprovalDecision(True),
            record=lambda kind, payload: records.append({"type": kind, "payload": payload}),
            events_since=lambda cursor: (messages[cursor:], len(messages)),
            access_store=SandboxAccessStore(
                root=folders["project"], session_id=session, settings=settings
            ),
        )

    script = folders["project"] / "probe.cjs"
    script.write_text(
        """const fs=require('node:fs'),a=require('node:assert/strict'),p=require('node:path');
const folders=FOLDERS, mode=process.argv[2];
function denied(f){a.throws(f,e=>['EACCES','EPERM'].includes(e.code));}
const source=p.join(folders.assets,'canary.txt'),write=p.join(folders.assets,'written.txt');
if(mode==='denied')denied(()=>fs.readFileSync(source));
else {a.equal(fs.readFileSync(source,'utf8'),'assets');
 if(mode==='write')fs.writeFileSync(write,'approved');else denied(()=>fs.writeFileSync(write,'forbidden'));}
for(const name of ['sibling','config','data','memory'])denied(()=>fs.readFileSync(p.join(folders[name],'canary.txt')));
console.log('NATIVE_SCOPE_'+mode.toUpperCase()+'_OK');
""".replace("FOLDERS", json.dumps({name: str(path) for name, path in folders.items()})),
        encoding="utf-8",
    )

    def run(access, mode, proposal=None):
        command = f"& '{str(node).replace(chr(39), chr(39) * 2)}' './probe.cjs' '{mode}'; exit $LASTEXITCODE"
        grant = access.prepare(proposal, command=command, tool="shell_run")
        grant.check()
        assert grant.settings.backend == "windows-mxc"
        result = build_shell_runner_from_settings(grant.settings, folders["project"]).run(
            root=folders["project"], cwd=folders["project"], cmd=command, timeout_s=20
        )
        assert result.returncode == 0 and "NATIVE_SCOPE_" in result.stdout, (
            result.stdout + result.stderr
        )

    def passed(name):
        report["checks"].append(name)
        print(name, flush=True)

    try:
        access = manager()
        run(access, "denied")
        read = {"read_folders": [str(folders["assets"])]}
        run(access, "read", read)
        run(access, "denied")
        passed("operation read access expires; source writes, siblings and private state denied")
        run(access, "read", {**read, "scope": "turn"})
        count = len(approvals)
        run(access, "read")
        assert len(approvals) == count
        messages.append({"type": "user_message"})
        run(access, "denied")
        passed("turn access reuses only permissions and expires on the next user message")
        run(access, "write", {"write_folders": [str(folders["assets"])], "scope": "session"})
        run(manager(), "write")
        run(manager("other"), "denied")
        manager().manage({"action": "revoke"})
        run(manager(), "denied")
        passed("session access survives restart, remains chat-bound and revokes durably")
        owner = manager("owner")
        run(owner, "read", {**read, "save_as": "assets"})
        assert approvals[-1].metadata["mandatory_explicit_approval"]
        run(manager("consumer"), "read", {"profile": "assets", "scope": "session"})
        owner.manage({"action": "revoke", "profile": "assets"})
        run(manager("consumer"), "denied")
        passed("saved profiles require explicit approval; revocation reaches restarted consumers")

        # Exercise the public agent-tool assembly as well as the grant manager.
        # A passing private store alone cannot prove the model can use it.
        class Host(NoopSurface):
            host_managed_approvals = True

            def request_approval(self, request):
                approvals.append(request)
                return ApprovalDecision(True)

        def tools():
            store = SessionStore(
                enabled=False,
                sessions_dir=folders["data"],
                session_id="tool-dispatch",
                cwd=str(folders["project"]),
                repo_root=str(folders["project"]),
            )
            return build_tools(
                root=folders["project"],
                console=Console(file=io.StringIO()),
                surface=Host(),
                store=store,
                mode="review",
                yes=False,
                shell_runner=build_shell_runner_from_settings(settings, folders["project"]),
                cfg=AppConfig(
                    extra_fields={
                        "shell_sandbox": {
                            "backend": "windows-mxc",
                            "mode": "strict",
                            "network": "off",
                            "filesystem_access": "workspace-write",
                            "windows_read_roots": [str(node.parent)],
                        }
                    }
                ),
                runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
                verification_enabled=False,
            )

        exposed = tools()
        schema = exposed["shell_run"].parameters["properties"]["permissions"]["properties"]
        assert (
            "session" in schema["scope"]["enum"]
            and "save_as" in schema
            and "sandbox_permissions" in exposed
        )
        for selected, mode, permission in [
            (exposed, "read", {**read, "scope": "session"}),
            (None, "read", None),
            (None, "denied", None),
        ]:
            active = selected or tools()
            if mode == "denied":
                active["sandbox_permissions"].run({"action": "revoke"})
            command = f"& '{str(node).replace(chr(39), chr(39) * 2)}' './probe.cjs' '{mode}'; exit $LASTEXITCODE"
            result = active["shell_run"].run(
                {"cmd": command, **({"permissions": permission} if permission else {})}
            )
            assert result["exit_code"] == 0 and "NATIVE_SCOPE_" in result["stdout"], result
        passed(
            "public agent tools expose session grants, restore them and revoke before native execution"
        )
        run(manager("identity"), "read", {**read, "scope": "session"})
        # Both paths are fixed children of this disposable evidence directory.
        assert folders["assets"].resolve().parent == output
        previous = output / "original-assets"
        assert previous.parent == output and not previous.exists()
        folders["assets"].rename(previous)
        folders["assets"].mkdir()
        try:
            manager("identity").prepare(None, command="must not launch", tool="shell_run")
        except ConfigError as exc:
            assert "changed" in str(exc)
        else:
            raise AssertionError("Replaced folder reused saved authority")
        passed("replaced folder identity is rejected before process launch")
        report["passed"] = True
    finally:
        report["approvalCount"] = len(approvals)
        (output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (output / "events.json").write_text(json.dumps(records, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
