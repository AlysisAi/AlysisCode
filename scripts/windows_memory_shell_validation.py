"""Disposable installed-engine probes; no sandbox or policy mocks."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import traceback
import uuid
from dataclasses import replace
from pathlib import Path

from alysis_code.sandbox_runner import build_shell_runner_from_settings
from alysis_code.sandbox_settings import ShellSandboxSettings


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    output = Path(sys.argv[1]).resolve()
    output.mkdir(exist_ok=False)
    roots = [output / name for name in ["Primary", "Design assets", "Σημειώσεις"]]
    outside = output / "Unselected"
    private = output / "Private engine"
    tokens = [str(uuid.uuid4()) for _ in roots]
    for folder, token in zip(roots, tokens, strict=True):
        folder.mkdir()
        (folder / "canary.txt").write_text(token, encoding="utf-8")
        # Test denial against existing metadata, not ENOENT after the previous
        # writable command removes its temporary protection reservations.
        (folder / ".git").mkdir()
        (folder / ".git" / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
        (folder / "AGENTS.md").write_text("Synthetic protected instructions\n", encoding="utf-8")
    for folder in [outside, private]:
        folder.mkdir()
        (folder / "canary.txt").write_text(str(uuid.uuid4()), encoding="utf-8")
    (output / "parent-canary.txt").write_text(str(uuid.uuid4()))
    os.environ["ALYSIS_CONFIG_DIR"] = str(private)
    os.environ["ALYSIS_DATA_DIR"] = str(private / "data")
    # Prove private memory stays excluded even beneath an explicitly writable
    # project root, for both the command and its real child process.
    memory = roots[1] / "Private memory"
    memory.mkdir()
    (memory / "canary.txt").write_text(str(uuid.uuid4()), encoding="utf-8")
    os.environ["ALYSIS_MEMORY_HOME"] = str(memory)
    node = Path(shutil.which("node")).resolve(strict=True)
    settings = ShellSandboxSettings(
        backend="windows-mxc",
        mode="strict",
        network="off",
        filesystem_access="workspace-write",
        windows_read_roots=(str(node.parent),),
        additional_workspace_roots=tuple(str(p) for p in roots[1:]),
    )
    report = {
        "executable": sys.executable,
        "node": str(node),
        "backend": settings.backend,
        "results": [],
    }

    def record(name, action):
        try:
            detail = action()
            report["results"].append({"name": name, "status": "pass", "detail": detail})
        except Exception:
            report["results"].append(
                {"name": name, "status": "fail", "traceback": traceback.format_exc()}
            )
        print(name, report["results"][-1]["status"], flush=True)
        (output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    def command(script):
        return (
            "& '"
            + str(node).replace("'", "''")
            + "' '"
            + str(script).replace("'", "''")
            + "'; exit $LASTEXITCODE"
        )

    def run(script, selected=settings, cwd=roots[1], cancel=None):
        runner = build_shell_runner_from_settings(selected, roots[0])
        result = runner.run(
            root=roots[0], cwd=cwd, cmd=command(script), timeout_s=45, cancel_check=cancel
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "runner": type(runner).__name__,
        }

    def file_operations(access):
        script = roots[0] / (access + ".cjs")
        blocked = [
            outside / "canary.txt",
            private / "canary.txt",
            output / "parent-canary.txt",
            memory / "canary.txt",
        ]
        script.write_text(
            "const fs=require('node:fs'),cp=require('node:child_process'),p=require('node:path'),assert=require('node:assert/strict');\n"
            + "const roots="
            + json.dumps([str(x) for x in roots])
            + ",tokens="
            + json.dumps(tokens)
            + ";\n"
            + "function denied(f){try{f()}catch(e){if(['EACCES','EPERM'].includes(e.code))return;throw e}throw Error('ACCESS ESCAPED')}\n"
            + "for(let i=0;i<roots.length;i++){const r=roots[i];assert.equal(fs.readFileSync(p.join(r,'canary.txt'),'utf8'),tokens[i]);"
            + (
                "const a=p.join(r,'created.txt'),b=p.join(r,'copy.txt'),c=p.join(r,'moved.txt');fs.writeFileSync(a,'created');fs.writeFileSync(a,'edited');fs.copyFileSync(a,b);fs.renameSync(b,c);assert.equal(fs.readFileSync(c,'utf8'),'edited');"
                if access == "workspace-write"
                else "denied(()=>fs.writeFileSync(p.join(r,'canary.txt'),'bad'));denied(()=>fs.mkdirSync(p.join(r,'must-not-create')));"
            )
            + "denied(()=>fs.writeFileSync(p.join(r,'.git','config'),'bad'));denied(()=>fs.writeFileSync(p.join(r,'AGENTS.md'),'bad'));}\n"
            + "for(const f of "
            + json.dumps([str(x) for x in blocked])
            + "){denied(()=>fs.readFileSync(f));denied(()=>fs.writeFileSync(f,'bad'));}\n"
            + "if(!process.argv.includes('child')){let c=cp.spawnSync(process.execPath,[__filename,'child'],{cwd:roots[2],encoding:'utf8'});assert.equal(c.status,0,c.stderr+c.stdout);}console.log('REAL_MXC_THREE_FOLDERS_OK');",
            encoding="utf-8",
        )
        protected = [p / name for p in roots for name in [".git/config", "AGENTS.md"]]
        before = {p: p.read_bytes() for p in [*blocked, *protected]}
        detail = run(script, replace(settings, filesystem_access=access))
        assert all(p.read_bytes() == data for p, data in before.items())
        assert all(
            (p / "canary.txt").read_text() == token for p, token in zip(roots, tokens, strict=True)
        )
        if access == "workspace-write":
            assert all(
                (p / "moved.txt").read_text() == "edited" and not (p / "copy.txt").exists()
                for p in roots
            )
        else:
            assert all(not (p / "must-not-create").exists() for p in roots)
        return detail

    for access in ["workspace-write", "read-only"]:
        record("three-folders-and-child-" + access, lambda access=access: file_operations(access))

    def revoked():
        script = roots[0] / "revoked.cjs"
        script.write_text(
            "const fs=require('node:fs');try{fs.readFileSync("
            + json.dumps(str(roots[1] / "canary.txt"))
            + ");throw Error('ACCESS ESCAPED')}catch(e){if(!['EACCES','EPERM'].includes(e.code))throw e}console.log('REVOKED');"
        )
        return run(
            script, replace(settings, additional_workspace_roots=(str(roots[2]),)), cwd=roots[0]
        )

    record("fresh-native-job-revokes-additional-folder", revoked)

    def cancelled():
        from alysis_code.process_reaping import CommandCancelledError

        script = roots[0] / "cancel.cjs"
        child = roots[0] / "child.cjs"
        marker = roots[0] / "child-ready.txt"
        heartbeat = roots[0] / "heartbeat.txt"
        child.write_text(
            "const fs=require('node:fs');fs.writeFileSync("
            + json.dumps(str(marker))
            + ",String(process.pid));setInterval(()=>fs.writeFileSync("
            + json.dumps(str(heartbeat))
            + ",String(Date.now())),50);"
        )
        script.write_text(
            "const cp=require('node:child_process');cp.spawn(process.execPath,["
            + json.dumps(str(child))
            + "],{stdio:'inherit'});setInterval(()=>{},1000);"
        )
        try:
            run(script, cwd=roots[0], cancel=lambda: marker.exists() and heartbeat.exists())
        except CommandCancelledError:
            pass
        else:
            raise AssertionError("Expected cancellation acknowledgment")
        pid = int(marker.read_text())
        old = heartbeat.read_bytes()
        time.sleep(1)
        assert heartbeat.read_bytes() == old, "Child continued after cancellation"
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            try:
                code = wintypes.DWORD()
                assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                assert code.value != 259, "Child still active"
            finally:
                kernel.CloseHandle(handle)
        else:
            assert ctypes.get_last_error() == 87, "Could not verify child exit"
        followup = roots[0] / "followup.cjs"
        followup.write_text(
            "require('node:fs').writeFileSync('after-cancel.txt','ok');console.log('AFTER_CANCEL_OK');"
        )
        detail = run(followup, cwd=roots[0])
        assert (roots[0] / "after-cancel.txt").read_text() == "ok"
        return {**detail, "childPid": pid, "heartbeatStopped": True}

    record("cancel-drains-real-child-and-next-operation-works", cancelled)
    return int(any(r["status"] != "pass" for r in report["results"]))


if __name__ == "__main__":
    raise SystemExit(main())
