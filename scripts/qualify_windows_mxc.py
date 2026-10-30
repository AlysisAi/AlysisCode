"""Bounded strict-MXC qualification in synthetic workspaces; never provisions accounts.

Requires the opt-in --features mxc helper. Retains failures and exact binary hashes.
An unsupported host is reported separately and is never counted as native success.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import uuid
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from alysis_code.windows_mxc_contract import WINDOWS_RUNTIME_DIRECTORIES  # noqa: E402


def owner() -> dict[str, int]:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(t) for t in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "parent_pid": os.getpid(),
        "parent_created": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-unsupported", action="store_true")
    parser.add_argument("--internet", action="store_true")
    parser.add_argument("--lifecycle", action="store_true")
    parser.add_argument("--filesystem-races", action="store_true")
    parser.add_argument("--directory-discovery", action="store_true")
    parser.add_argument("--network-boundary", action="store_true")
    parser.add_argument("--platform-boundary", action="store_true")
    parser.add_argument("--ui-boundary", action="store_true")
    parser.add_argument("--nested-probe", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Native Windows is required")
    helper = args.helper.resolve(strict=True)
    node = Path(shutil.which("node") or "").resolve(strict=True)
    result = (
        args.output or REPO / ".qa-results/0142-integration" / ("mxc-native-" + uuid.uuid4().hex)
    ).resolve()
    result.mkdir(parents=True, exist_ok=False)
    work = result / "workspace"
    outside = result / "outside"
    for directory in [work, outside, work / ".git", work / ".tmp"]:
        directory.mkdir()
    (outside / "canary").write_text("outside-original")
    (work / ".git/canary").write_text("protected-original")
    shutil.copyfile(REPO / "tests/fixtures/windows_mxc_tools.cjs", work / "probe.cjs")
    (work / "sample.test.cjs").write_text(
        "const t=require('node:test');const a=require('node:assert/strict');t('isolated child',()=>a.equal(2+2,4));"
    )
    system = Path(os.environ["SystemRoot"]) / "System32"
    env = {k: os.environ[k] for k in ["SystemRoot", "LOCALAPPDATA"]}
    env.update(
        {
            "PATH": os.pathsep.join(map(str, [system, node.parent])),
            "TEMP": str(work / ".tmp"),
            "TMP": str(work / ".tmp"),
            "NODE_OPTIONS": "--preserve-symlinks --preserve-symlinks-main",
            "CI": "1",
        }
    )
    events = []
    summary = {
        "scope": "Alysis opt-in direct MXC helper, bounded synthetic fixtures; not production activation",
        "recorded_utc": datetime.now(timezone.utc).isoformat(),
        "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
        "node_sha256": hashlib.sha256(node.read_bytes()).hexdigest(),
        "workspace": str(work),
        "native_qualified": False,
        "requested_scopes": {
            "lifecycle": args.lifecycle,
            "internet": args.internet,
            "filesystem_races": args.filesystem_races,
            "directory_discovery": args.directory_discovery,
            "network_boundary": args.network_boundary,
            "platform_boundary": args.platform_boundary,
            "ui_boundary": args.ui_boundary,
            "nested_boundary": args.nested_probe is not None,
        },
        "test_source_sha256": {
            str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                Path(__file__),
                REPO / "src/alysis_code/windows_mxc_contract.py",
                REPO / "scripts/windows_mxc_lifecycle.py",
                REPO / "scripts/windows_mxc_filesystem.py",
                REPO / "scripts/windows_mxc_network.py",
                REPO / "scripts/windows_mxc_platform.py",
                REPO / "scripts/windows_mxc_ui.py",
                REPO / "scripts/windows_mxc_nested.py",
                REPO / "native/windows-sandbox/examples/mxc_nested_probe.rs",
                REPO / "tests/fixtures/windows_mxc_tools.cjs",
                REPO / "tests/fixtures/windows_mxc_family.cjs",
                REPO / "tests/fixtures/windows_mxc_reparse.py",
                REPO / "tests/fixtures/windows_mxc_platform.ps1",
                REPO / "tests/fixtures/windows_mxc_platform_child.cjs",
                REPO / "tests/fixtures/windows_mxc_ui.py",
            ]
        },
        "events": events,
    }

    def save():
        (result / "result.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    def run(name, argv, request=None, expected=0, timeout=30):
        completed = subprocess.run(
            [str(a) for a in argv],
            cwd=work,
            input=None if request is None else json.dumps(request).encode(),
            capture_output=True,
            timeout=timeout,
        )
        event = {
            "name": name,
            "exit_code": completed.returncode,
            "expected_exit": expected,
            "stdout": completed.stdout.decode(errors="replace")[:16000],
            "stderr": completed.stderr.decode(errors="replace")[:8000],
        }
        events.append(event)
        save()
        print(json.dumps(event), flush=True)
        if completed.returncode != expected:
            raise AssertionError(f"{name}: unexpected exit {completed.returncode}")
        return event

    def request(argv, **overrides):
        value = {
            "protocol": 4,
            "cwd": str(work),
            "argv": list(map(str, argv)),
            "read": [
                str(work),
                str(node.parent),
                *[
                    str(Path(os.environ["SystemRoot"]) / name)
                    for name in WINDOWS_RUNTIME_DIRECTORIES
                    if (Path(os.environ["SystemRoot"]) / name).exists()
                ],
            ],
            "write": [str(work)],
            "enumerate": [work.anchor] if args.directory_discovery else [],
            "deny_read": [str(outside)],
            "deny_write": [str(work / ".git")],
            "env": list(env.items()),
            "network": "off",
            "timeout_ms": 15000,
            **owner(),
        }
        value.update(overrides)
        return value

    def contained(name, argv, expected=0, **overrides):
        return run(name, [helper, "mxc-run-request"], request(argv, **overrides), expected=expected)

    try:
        run("protocol", [helper, "mxc-protocol"])
        probe = subprocess.run([str(helper), "mxc-probe"], capture_output=True, timeout=15)
        available = json.loads(probe.stdout)["available"]
        summary["native_available"] = available
        if not available:
            contained(
                "unsupported_native_refuses_workload",
                [system / "cmd.exe", "/d", "/c", "echo", "MUST_NOT_RUN"],
                expected=125,
            )
            assert "MUST_NOT_RUN" not in events[-1]["stdout"]
            summary["status"] = "unsupported_host_refusal_passed"
            save()
            return 0 if args.allow_unsupported else 2
        contained("command_launch", [system / "cmd.exe", "/d", "/c", "echo", "MXC_DIRECT_OK"])
        run(
            "host_process_access_control",
            [helper, "mxc-process-access-probe", str(os.getpid()), "allow"],
        )
        contained(
            "contained_host_process_access_denied",
            [helper, "mxc-process-access-probe", str(os.getpid()), "deny"],
            read=[str(work), os.environ["SystemRoot"], str(node.parent), str(helper)],
        )
        event = contained(
            "node_filesystem_children_ipc_offline", [node, work / "probe.cjs", outside]
        )
        rows = [json.loads(line) for line in event["stdout"].splitlines()]
        expected = {
            "workspace_rw",
            "workspace_rename_replace",
            "workspace_directory_rename",
            "workspace_file_directory_delete",
            "outside_rename",
            "outside_rename_source_preserved",
            "outside_read",
            "outside_write",
            "git_write",
            "git_rename",
            "git_delete",
            "git_hardlink_creation",
            "git_file_rename_in",
            "git_directory_rename_in",
            "git_hardlink_in",
            "spawnSync",
            "spawn",
            "fork_ipc",
            "offline_numeric_tcp",
        }
        assert {row["name"] for row in rows} == expected and all(row["ok"] for row in rows)
        contained("node_default_isolated_test_runner", [node, "--test", "./sample.test.cjs"])
        contained(
            "powershell_workspace",
            [
                system / "WindowsPowerShell/v1.0/powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; Set-Content -LiteralPath './ps.txt' -Value 'PS_OK'; Get-Content -LiteralPath './ps.txt'",
            ],
        )
        assert (work / "ps.txt").read_text().strip() == "PS_OK"
        denied = "try{require('node:fs').writeFileSync('denied.txt','bad');process.exitCode=1}catch(e){if(!['EPERM','EACCES'].includes(e.code))process.exitCode=2;console.log(e.code)}"
        contained("readonly_workspace", [node, "-e", denied], write=[])
        assert not (work / "denied.txt").exists()
        future = [str(work / ".new-protected"), str(work / "POLICY.md")]
        future_code = (
            "const fs=require('node:fs');let failures=0;"
            "for(const [kind,p] of [['dir','.new-protected'],['file','POLICY.md']]){"
            "try{if(kind==='dir')fs.mkdirSync(p);else fs.writeFileSync(p,'bad');failures++}"
            "catch(e){if(!['EACCES','EPERM'].includes(e.code))failures++;console.log(kind+':'+e.code)}}"
            "process.exitCode=failures?1:0"
        )
        contained(
            "unreserved_metadata_refuses_workload",
            [node, "-e", future_code],
            expected=125,
            deny_write=[str(work / ".git"), *future],
        )
        assert all(not Path(path).exists() for path in future)
        # The host reserves the synthetic names; PSEC must then protect the
        # actual objects, including writes below an initially empty directory.
        Path(future[0]).mkdir()
        Path(future[1]).touch()
        reserved_code = (
            "const fs=require('node:fs');let failures=0;"
            "for(const p of ['.new-protected/payload','POLICY.md']){"
            "try{fs.writeFileSync(p,'bad');failures++}"
            "catch(e){if(!['EACCES','EPERM'].includes(e.code))failures++;console.log(e.code)}}"
            "process.exitCode=failures?1:0"
        )
        contained(
            "reserved_metadata_write_denied",
            [node, "-e", reserved_code],
            deny_write=[str(work / ".git"), *future],
        )
        assert not (Path(future[0]) / "payload").exists()
        assert Path(future[1]).read_bytes() == b""
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(2)
            port = listener.getsockname()[1]
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                pass
            code = f"const s=require('node:net').connect({{host:'127.0.0.1',port:{port}}});s.setTimeout(2500,()=>{{s.destroy();process.exitCode=2}});s.on('connect',()=>{{s.destroy();process.exitCode=1}});s.on('error',e=>{{console.log(e.code);if(!['EACCES','EPERM'].includes(e.code))process.exitCode=3}})"
            contained("online_host_loopback_denied", [node, "-e", code], network="on")
        contained(
            "stale_parent_refused",
            [system / "cmd.exe", "/d", "/c", "echo", "MUST_NOT_RUN"],
            expected=125,
            parent_created=1,
            deny_write=[str(work / ".stale-parent-reservation") + "\\"],
        )
        assert "MUST_NOT_RUN" not in events[-1]["stdout"]
        assert not (work / ".stale-parent-reservation").exists()
        invalid = work / ".invalid-request-reservation"
        contained(
            "invalid_protection_plan_has_no_partial_effects",
            [system / "cmd.exe", "/d", "/c", "echo", "MUST_NOT_RUN"],
            expected=125,
            deny_write=[str(invalid) + "\\", str(outside / "not-allowed") + "\\"],
        )
        assert "MUST_NOT_RUN" not in events[-1]["stdout"]
        assert not invalid.exists() and not (outside / "not-allowed").exists()
        contained("timeout", [node, "-e", "setInterval(()=>{},1000)"], expected=124, timeout_ms=200)
        if args.lifecycle:
            from windows_mxc_lifecycle import qualify

            def record(event):
                events.append(event)
                save()
                print(json.dumps(event), flush=True)

            qualify(helper, node, work, request, record)
        if args.internet:
            curl = [
                system / "curl.exe",
                "--disable",
                "--max-time",
                "10",
                "--silent",
                "--show-error",
                "--output",
                "NUL",
                "--write-out",
                "HTTPS_STATUS=%{http_code}",
                "https://example.com/",
            ]
            assert "HTTPS_STATUS=200" in run("host_https_control", curl)["stdout"]
            assert (
                "HTTPS_STATUS=200"
                in contained("public_outbound_schannel_https", curl, network="on")["stdout"]
            )
            # Bypass DNS for the offline transport check. Otherwise curl may
            # fail at name resolution without reaching the egress boundary.
            address = socket.gethostbyname("example.com")
            assert ipaddress.ip_address(address).is_global
            exact_endpoint = [*curl[:-1], "--resolve", f"example.com:443:{address}", curl[-1]]
            assert "HTTPS_STATUS=200" in run("host_https_exact_endpoint", exact_endpoint)["stdout"]
            offline = contained(
                "public_outbound_off_refused", exact_endpoint, network="off", expected=7
            )
            assert "HTTPS_STATUS=000" in offline["stdout"]
            assert (
                "HTTPS_STATUS=200"
                in run("host_https_control_after_offline", exact_endpoint)["stdout"]
            )
        if args.network_boundary:
            from windows_mxc_network import qualify as qualify_network

            def record_network(event):
                events.append(event)
                save()
                print(json.dumps(event), flush=True)

            qualify_network(helper, node, work, request, record_network)
        if args.filesystem_races:
            from windows_mxc_filesystem import qualify as qualify_filesystem

            def record_filesystem(event):
                events.append(event)
                save()
                print(json.dumps(event), flush=True)

            qualify_filesystem(helper, node, work, request, record_filesystem)
        if args.platform_boundary:
            from windows_mxc_platform import qualify as qualify_platform

            def record_platform(event):
                events.append(event)
                save()
                print(json.dumps(event), flush=True)

            qualify_platform(node, work, request, contained, record_platform)
        if args.ui_boundary:
            from windows_mxc_ui import qualify as qualify_ui

            def record_ui(event):
                events.append(event)
                save()
                print(json.dumps(event), flush=True)

            qualify_ui(work, request, contained, record_ui)
        if args.nested_probe:
            from windows_mxc_nested import qualify as qualify_nested

            def record_nested(event):
                events.append(event)
                save()
                print(json.dumps(event), flush=True)

            qualify_nested(args.nested_probe, work, contained, record_nested)
        assert (outside / "canary").read_text() == "outside-original"
        assert (work / ".git/canary").read_text() == "protected-original"
        summary["status"] = "bounded_tool_checks_passed"
        summary["canaries_unchanged"] = True
        summary["native_qualified"] = True
        summary["qualification_limit"] = (
            "Only listed scenarios on this host. General races, other-machine networking and product/installed qualification remain."
        )
        return 0
    except Exception as error:
        summary["status"] = "failed"
        summary["error"] = str(error)
        raise
    finally:
        save()
        print(
            json.dumps({"evidence": str(result / "result.json"), "status": summary.get("status")}),
            flush=True,
        )


if __name__ == "__main__":
    sys.exit(main())
