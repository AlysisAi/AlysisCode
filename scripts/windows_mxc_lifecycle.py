"""Original-handle observations for bounded MXC synthetic process families."""

from __future__ import annotations

import ctypes
import json
import shutil
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path


class OriginalProcess:
    def __init__(self, pid):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            *([ctypes.POINTER(wintypes.FILETIME)] * 4),
        ]
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.OpenProcess(0x1000 | 0x100000 | 1, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.pid = pid
        times = [wintypes.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(self.handle, *(ctypes.byref(t) for t in times)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())
        self.created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        if self.kernel.WaitForSingleObject(self.handle, 0) != 258:
            self.close()
            raise AssertionError("fixture process was not originally live")

    def wait(self, ms=10000):
        return self.kernel.WaitForSingleObject(self.handle, ms) == 0

    def terminate(self):
        if not self.wait(0) and not self.kernel.TerminateProcess(self.handle, 126):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def qualify(helper: Path, node: Path, work: Path, request, record):
    source = Path(__file__).resolve().parents[1] / "tests/fixtures/windows_mxc_family.cjs"
    shutil.copyfile(source, work / "family.cjs")
    for mode in ["foreground_exit", "helper_death", "engine_death", "deadline"]:
        case = work / ("family-" + mode)
        case.mkdir()
        reserved = work / (".reserved-" + mode)
        renamed = work / (".renamed-" + mode)
        nonce = uuid.uuid4().hex
        payload = request(
            [node, work / "family.cjs", "root", case, nonce],
            timeout_ms=5000 if mode == "deadline" else 15000,
        )
        payload["deny_write"].append(str(reserved) + "\\")
        originals = []
        process = None
        helper_original = None
        try:
            if mode == "engine_death":
                # The temporary engine owns the helper through the same stdin protocol.
                # It loads only this repository's trusted test code, outside the grant.
                worker = (
                    "import json,subprocess,sys;sys.path.insert(0,sys.argv[2]);"
                    "from qualify_windows_mxc import owner;"
                    "r=json.load(sys.stdin);r.update(owner());"
                    "p=subprocess.Popen([sys.argv[1],'mxc-run-request'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
                    "p.stdin.write(json.dumps(r).encode());p.stdin.close();"
                    "print(json.dumps({'helper_pid':p.pid}),flush=True);p.wait()"
                )
                process = subprocess.Popen(
                    [sys.executable, "-c", worker, str(helper), str(Path(__file__).parent)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=work,
                )
            else:
                process = subprocess.Popen(
                    [str(helper), "mxc-run-request"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=work,
                )
            process.stdin.write(json.dumps(payload).encode())
            process.stdin.close()
            process.stdin = None
            deadline = time.monotonic() + 10
            rows = {}
            while len(rows) < 3:
                if process.poll() is not None:
                    raise AssertionError(f"{mode}: helper/engine exited before live checkpoint")
                for role in ["root", "child", "grandchild"]:
                    if role in rows:
                        continue
                    try:
                        row = json.loads((case / (role + ".json")).read_text())
                    except (FileNotFoundError, json.JSONDecodeError):
                        continue
                    assert row["nonce"] == nonce
                    rows[role] = row
                    originals.append(OriginalProcess(row["pid"]))
                if time.monotonic() > deadline:
                    raise AssertionError(f"{mode}: family never became live")
                time.sleep(0.01)
            assert rows["child"]["parent"] == rows["root"]["pid"]
            assert rows["grandchild"]["parent"] == rows["child"]["pid"]
            assert reserved.is_dir() and list(reserved.iterdir()) == []
            identity = (reserved.stat().st_dev, reserved.stat().st_ino)
            try:
                reserved.rename(renamed)
            except OSError as error:
                assert error.winerror == 32, f"unexpected reservation lock failure: {error}"
            else:
                renamed.rename(reserved)
                raise AssertionError("live reservation was not held by original handles")
            if mode == "engine_death":
                # Worker prints this before waiting; the live family proves it ran.
                helper_pid = json.loads(process.stdout.readline())["helper_pid"]
                helper_original = OriginalProcess(helper_pid)
                process.kill()
            elif mode == "helper_death":
                process.kill()
            elif mode == "foreground_exit":
                (case / "release-root").write_text("release")
            stdout, stderr = process.communicate(timeout=15)
            if mode == "foreground_exit":
                assert process.returncode == 0, stderr.decode(errors="replace")
            if mode == "deadline":
                assert process.returncode == 124, stderr.decode(errors="replace")
            all_exited = all(original.wait() for original in originals)
            assert all_exited, f"{mode}: an original descendant survived"
            if helper_original:
                assert helper_original.wait(), "helper survived original engine death"
            # Surviving empty reservations need no pathname cleanup or ACL
            # rollback. Original handles must close after exit so reuse works.
            reserved.rename(renamed)
            renamed.rename(reserved)
            assert (reserved.stat().st_dev, reserved.stat().st_ino) == identity
            assert list(reserved.iterdir()) == []
            reuse = request(
                [node, "-e", "console.log('RESERVATION_REUSED')"],
                deny_write=payload["deny_write"],
            )
            reused = subprocess.run(
                [str(helper), "mxc-run-request"],
                input=json.dumps(reuse).encode(),
                capture_output=True,
                timeout=15,
                cwd=work,
            )
            assert reused.returncode == 0 and b"RESERVATION_REUSED" in reused.stdout, reused.stderr
            event = {
                "name": "lifecycle_" + mode,
                "ok": True,
                "original_live_processes": [
                    {"pid": p.pid, "created": p.created, "exit_observed": p.wait(0)}
                    for p in originals
                ],
                "helper_or_owner_exit_code": process.returncode,
                "reservation": {
                    "locked_while_live": True,
                    "released_after_exit": True,
                    "identity_unchanged": True,
                    "next_command_passed": True,
                },
            }
            record(event)
        except Exception as error:
            record({"name": "lifecycle_" + mode, "ok": False, "error": str(error)})
            raise
        finally:
            if process and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            for original in [*originals, *([helper_original] if helper_original else [])]:
                try:
                    if not original.wait(0):
                        original.terminate()
                        original.wait()
                finally:
                    original.close()
