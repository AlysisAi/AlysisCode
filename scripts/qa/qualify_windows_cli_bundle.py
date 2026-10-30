"""Local, provider-free qualification of an installed standalone Windows CLI.

Run this file using a clean copied installation's Python with -I. No Desktop
environment, helper overrides, global configuration or administrator is needed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("Run the installed qualification on Windows")
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    for key in list(os.environ):
        if key.upper().startswith(("ALYSIS_", "SYLLIPTOR_")):
            del os.environ[key]
    os.environ.update(
        ALYSIS_CONFIG_DIR=str(output / "config"),
        ALYSIS_DATA_DIR=str(output / "data"),
        ALYSIS_TERMINAL_OWNERSHIP_DIR=str(output / "ownership"),
    )
    import alysis_code
    from alysis_code.background_runner import build_background_shell_runner_from_settings
    from alysis_code.config import AppConfig
    from alysis_code.contained_read_tools import ContainedReadTools
    from alysis_code.sandbox_doctor import diagnose_sandbox
    from alysis_code.sandbox_runner import build_shell_runner_from_settings
    from alysis_code.sandbox_settings import resolve_shell_sandbox_settings
    from alysis_code.windows_mxc import resolve_helper

    if "site-packages" not in Path(alysis_code.__file__).parts:
        raise SystemExit("Qualification must import the installed package, not repository source")
    evidence = {
        "package": str(Path(alysis_code.__file__).parent),
        "python": sys.executable,
        "desktopRequired": False,
        "githubActionsUsed": False,
        "checks": [],
        "passed": False,
    }

    def passed(name: str) -> None:
        evidence["checks"].append(name)

    try:
        settings = resolve_shell_sandbox_settings(AppConfig(model="fixture"))
        assert settings.backend == "windows-mxc" and settings.mode == "strict"
        helper = resolve_helper()
        assert "site-packages" in helper.parts
        evidence["helperSha256"] = hashlib.sha256(helper.read_bytes()).hexdigest()
        passed("default selection and bundled helper identity")
        diagnosis = diagnose_sandbox(AppConfig(model="fixture"), include_smoke=True)
        assert diagnosis.ready, diagnosis.checks
        passed("doctor execution, outside read/write denial and contained file workers")
        workspace = output / "workspace"
        workspace.mkdir()
        outside = output / "outside.txt"
        outside.write_text("unchanged")
        (workspace / "AGENTS.md").write_text("protected")
        node = workspace / "node.exe"
        shutil.copyfile(args.node, node)
        fixture = workspace / "probe.cjs"
        fixture.write_text(
            "const fs=require('node:fs'),cp=require('node:child_process');"
            "const outside=" + json.dumps(str(outside)) + ";"
            "function deny(fn){try{fn()}catch(e){if(['EACCES','EPERM'].includes(e.code))return;throw e}throw Error('escaped')}"
            "deny(()=>fs.readFileSync(outside));deny(()=>fs.writeFileSync(outside,'bad'));"
            "deny(()=>fs.writeFileSync('AGENTS.md','bad'));"
            "fs.writeFileSync('created.txt','inside');"
            "const child=cp.spawnSync(process.execPath,['-e',\"console.log('child-ok')\"],{encoding:'utf8',timeout:5000});"
            "if(child.status!==0||!child.stdout.includes('child-ok'))throw Error('child failed');"
            "console.log('standalone-ok');"
        )
        command = "& './node.exe' './probe.cjs'; exit $LASTEXITCODE"
        result = build_shell_runner_from_settings(settings, workspace).run(
            root=workspace, cwd=workspace, cmd=command, timeout_s=20
        )
        assert result.returncode == 0 and "standalone-ok" in result.stdout, (
            result.stdout,
            result.stderr,
        )
        assert outside.read_text() == "unchanged"
        assert (workspace / "AGENTS.md").read_text() == "protected"
        passed("foreground writes, protected metadata, outside denial and Node subprocess")
        tools = ContainedReadTools(settings, workspace)
        assert "created.txt" in str(tools.run("fs_list", {}))
        tools.run_write("fs_mkdir", {"path": "created-directory"})
        assert (workspace / "created-directory").is_dir()
        passed("installed contained file listing and mutation")
        readonly = replace(settings, filesystem_access="read-only")
        filename = str(workspace / "readonly-must-not-exist.txt").replace("'", "''")
        result = build_shell_runner_from_settings(readonly, workspace).run(
            root=workspace,
            cwd=workspace,
            cmd=f"try {{ [IO.File]::WriteAllText('{filename}','bad'); exit 45 }} catch {{ Write-Output 'denied' }}",
            timeout_s=20,
        )
        assert result.returncode == 0 and "denied" in result.stdout
        assert not (workspace / "readonly-must-not-exist.txt").exists()
        passed("read-only policy")
        background = build_background_shell_runner_from_settings(settings, workspace).start(
            root=workspace, cwd=workspace, cmd=command
        )
        try:
            stdout, stderr = background.popen.communicate(timeout=20)
            assert background.popen.returncode == 0 and b"standalone-ok" in stdout, (stdout, stderr)
        finally:
            background.cleanup()
        passed("background execution uses the same containment")
        started = time.monotonic()
        try:
            build_shell_runner_from_settings(settings, workspace).run(
                root=workspace, cwd=workspace, cmd="Start-Sleep -Seconds 30", timeout_s=1
            )
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError("Long-running command did not time out")
        assert time.monotonic() - started < 12
        result = build_shell_runner_from_settings(settings, workspace).run(
            root=workspace, cwd=workspace, cmd="Write-Output 'recovered'", timeout_s=15
        )
        assert result.returncode == 0 and "recovered" in result.stdout
        passed("timeout and immediate recovery")
        evidence["passed"] = True
    finally:
        (output / "result.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
