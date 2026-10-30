"""One hidden synthetic window with a known HWND; never enumerate real windows."""

import ctypes
import json
import shutil
import subprocess
import sys
import uuid
from ctypes import wintypes
from pathlib import Path


def qualify(work: Path, request, contained, record):
    user = ctypes.WinDLL("user32", use_last_error=True)
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
    user.DestroyWindow.argtypes = [wintypes.HWND]
    user.DestroyWindow.restype = wintypes.BOOL
    nonce = uuid.uuid4().hex
    # WS_POPUP without WS_VISIBLE: no UI is shown and no user window is touched.
    window = user.CreateWindowExW(
        0, "STATIC", nonce, 0x80000000, 0, 0, 10, 10, None, None, None, None
    )
    if not window:
        raise ctypes.WinError(ctypes.get_last_error())
    python = Path(sys.executable).resolve(strict=True)
    # The venv launcher needs the actual base installation. Select the canonical
    # base executable so a junction alias cannot bypass native path admission.
    python = Path(getattr(sys, "_base_executable", python)).resolve(strict=True)
    fixture = work / ("ui-" + uuid.uuid4().hex + ".py")
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "tests/fixtures/windows_mxc_ui.py", fixture
    )
    argv = [str(python), "-I", "-B", str(fixture), str(window), nonce]

    def control(name):
        result = subprocess.run(argv, capture_output=True, timeout=10)
        event = {
            "name": name,
            "exit_code": result.returncode,
            "stdout": result.stdout.decode(),
            "stderr": result.stderr.decode(errors="replace"),
        }
        record(event)
        assert result.returncode == 0 and json.loads(event["stdout"])["exact"], event

    try:
        control("host_ui_before")
        event = contained(
            "contained_host_window_text", argv, read=[*request([])["read"], str(python.parent)]
        )
        observed = json.loads(event["stdout"])
        control("host_ui_after")
        assert observed["length"] == 0 and not observed["exact"], observed
        record({"name": "ui_boundary_passed", "scope": "exact hidden host window caption only"})
    finally:
        if not user.DestroyWindow(window):
            raise ctypes.WinError(ctypes.get_last_error())
