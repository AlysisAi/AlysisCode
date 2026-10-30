"""Test a known hidden synthetic window; never enumerate or modify user windows."""

import argparse
import ctypes
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import uuid
from ctypes import wintypes
from pathlib import Path

from alysis_code import windows_sandbox
from alysis_code.sandbox_runner import build_shell_runner_from_settings
from alysis_code.sandbox_settings import ShellSandboxSettings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt" or not args.helper.is_absolute() or not args.helper.is_file():
        parser.error("An absolute native Windows MXC helper is required")
    if not args.output.is_absolute() or args.output.exists():
        parser.error("--output must be a new absolute evidence directory")
    root = args.output
    work = root / "workspace"
    work.mkdir(parents=True)
    for name in ("config", "data"):
        (root / name).mkdir()
        os.environ[f"ALYSIS_{name.upper()}_DIR"] = str(root / name)
    os.environ["ALYSIS_WINDOWS_MXC_HELPER"] = str(args.helper)
    os.environ["ALYSIS_WINDOWS_MXC_SHA256"] = hashlib.sha256(args.helper.read_bytes()).hexdigest()
    shutil.copyfile(__file__, root / "harness.py")
    python = Path(getattr(sys, "_base_executable", sys.executable)).resolve(strict=True)
    fixture = work / "set-caption.py"
    fixture.write_text(
        "import ctypes,json,sys; from ctypes import wintypes\n"
        "u=ctypes.WinDLL('user32',use_last_error=True)\n"
        "u.SetWindowTextW.argtypes=[wintypes.HWND,wintypes.LPCWSTR]\n"
        "u.SetWindowTextW.restype=wintypes.BOOL\n"
        "ctypes.set_last_error(0);result=ctypes.c_size_t()\n"
        "if sys.argv[3]=='api': ok=u.SetWindowTextW(int(sys.argv[1]),sys.argv[2])\n"
        "else:\n"
        " u.SendMessageTimeoutW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,"
        "wintypes.LPARAM,wintypes.UINT,wintypes.UINT,ctypes.POINTER(ctypes.c_size_t)]\n"
        " u.SendMessageTimeoutW.restype=ctypes.c_ssize_t\n"
        " text=ctypes.create_unicode_buffer(sys.argv[2])\n"
        " ok=u.SendMessageTimeoutW(int(sys.argv[1]),12,0,ctypes.addressof(text),34,3000,ctypes.byref(result))\n"
        "print(json.dumps({'ok':bool(ok),'error':ctypes.get_last_error(),'message_result':result.value}))\n",
        encoding="utf-8",
    )
    fixture_hash = hashlib.sha256(fixture.read_bytes()).hexdigest()
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user.CreateWindowExW.restype = wintypes.HWND
    user.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user.GetMessageW.restype = wintypes.BOOL
    user.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user.DispatchMessageW.restype = ctypes.c_ssize_t
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.PostThreadMessageW.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user.DestroyWindow.argtypes = [wintypes.HWND]
    identity = queue.Queue()
    errors = []
    initial = uuid.uuid4().hex

    def owner():
        window = user.CreateWindowExW(
            0, "STATIC", initial, 0x80000000, 0, 0, 10, 10, None, None, None, None
        )
        if not window:
            identity.put(None)
            return
        message = wintypes.MSG()
        identity.put((window, kernel.GetCurrentThreadId()))
        try:
            while True:
                status = user.GetMessageW(ctypes.byref(message), None, 0, 0)
                if status == 0:
                    break
                if status == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                user.TranslateMessage(ctypes.byref(message))
                user.DispatchMessageW(ctypes.byref(message))
        except Exception as error:
            errors.append(str(error))
        finally:
            if not user.DestroyWindow(window):
                errors.append("Owned synthetic window could not be destroyed")

    thread = threading.Thread(target=owner, daemon=True)
    thread.start()
    window_identity = identity.get(timeout=5)
    assert window_identity, "Hidden synthetic window creation failed"
    window, thread_id = window_identity
    record = {
        "status": "running",
        "root": str(root),
        "events": [],
        "helper_sha256": os.environ["ALYSIS_WINDOWS_MXC_SHA256"],
        "fixture_sha256": fixture_hash,
        "engine_module": windows_sandbox.__file__,
    }

    def caption():
        text = ctypes.create_unicode_buffer(128)
        user.GetWindowTextW(window, text, len(text))
        return text.value

    def run(label, operation, network=None):
        before = caption()
        nonce = uuid.uuid4().hex
        argv = [str(python), "-I", "-B", str(fixture), str(window), nonce, operation]
        if network is None:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        else:
            runner = build_shell_runner_from_settings(
                ShellSandboxSettings(
                    backend="windows-mxc", network=network, windows_read_roots=(str(python.parent),)
                ),
                work,
            )
            cmd = (
                "& "
                + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
                + "; exit $LASTEXITCODE"
            )
            result = runner.run(root=work, cwd=work, cmd=cmd, timeout_s=15)
        event = {
            "name": label,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        record["events"].append(event)
        assert result.returncode == 0, event
        observed = json.loads(result.stdout)
        after = caption()
        event["host_caption_unchanged"] = after == before
        event["host_caption_matches_attempt"] = after == nonce
        if network is None:
            assert observed["ok"] and after == nonce, event
        else:
            # SetWindowText may report success for an inaccessible USER object.
            # GetLastError is not authoritative after success. The external owner
            # must independently observe no mutation, bracketed by working host
            # controls; do not call this an explicit API access-denied result.
            assert after == before, "Contained child changed the host's synthetic window caption"
            if operation == "message":
                assert not observed["ok"] and observed["error"] in (5, 1400), event
            event["qualification"] = "exact-host-caption-unchanged"
        event["passed"] = True

    try:
        assert caption() == initial
        for operation in ("api", "message"):
            run(operation + "_host_before", operation)
            run(operation + "_contained_off", operation, "off")
            run(operation + "_contained_on", operation, "on")
            run(operation + "_host_after", operation)
        assert hashlib.sha256(fixture.read_bytes()).hexdigest() == fixture_hash
        record["status"] = "passed"
    except Exception as error:
        record["status"] = "failed"
        record["error"] = str(error)
        raise
    finally:
        user.PostThreadMessageW(thread_id, 0x0012, 0, 0)
        thread.join(timeout=5)
        record["cleanup_errors"] = errors
        record["owner_thread_stopped"] = not thread.is_alive()
        if errors or thread.is_alive():
            record["status"] = "failed"
        (root / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(json.dumps({"root": str(root), "status": record["status"]}))
        assert record["status"] == "passed", record


if __name__ == "__main__":
    main()
