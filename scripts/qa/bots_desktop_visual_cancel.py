"""Real X11 frame-wait positive control and cancellation, no model provider.

The observer wrapper records actual captures but does not replace capture, fresh,
dispatch or cancellation. A private native dialog writes a receipt only after its
visible button is activated. A separate private app changes the root pixels.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shlex
import tempfile
import time
from pathlib import Path

from alysis_code.bots.desktop_linux import OfflineLinuxDesktop
from alysis_code.cancellation import CooperativeCancellationError

OBSERVER = """import hashlib, runpy
from pathlib import Path
module=runpy.run_path('/opt/desktop_worker.py')
original=module['Computer'].capture
def capture(self):
    pixels=original(self)
    if Path('/workspace/arm-capture').exists():
        marker=Path('/workspace/capture-entered')
        if not marker.exists(): marker.write_text(hashlib.sha256(pixels).hexdigest())
    return pixels
module['Computer'].capture=capture
module['main']()
"""

PAINTER = """import ctypes, time
from pathlib import Path
x=ctypes.CDLL('libX11.so.6')
x.XOpenDisplay.argtypes=[ctypes.c_char_p];x.XOpenDisplay.restype=ctypes.c_void_p
x.XDefaultRootWindow.argtypes=[ctypes.c_void_p];x.XDefaultRootWindow.restype=ctypes.c_ulong
x.XSetWindowBackground.argtypes=[ctypes.c_void_p,ctypes.c_ulong,ctypes.c_ulong]
x.XClearWindow.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
x.XSync.argtypes=[ctypes.c_void_p,ctypes.c_int]
display=x.XOpenDisplay(None);assert display
window=x.XDefaultRootWindow(display)
previous=None
while True:
    state=Path('/workspace/color').read_text()
    if state!=previous:
        x.XSetWindowBackground(display,window,int(state,16));x.XClearWindow(display,window);x.XSync(display,0)
        Path('/workspace/painted').write_text(state);previous=state
    time.sleep(.01)
"""


def until(check, seconds=3):
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("Real desktop positive control did not become ready")
        time.sleep(0.02)


def study(cancelled, evidence_dir):
    with tempfile.TemporaryDirectory(prefix="bots-linux-night-20261019-visual-stop-") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        workspace.mkdir()
        observer = root / "observer.py"
        observer.write_text(OBSERVER)

        class RecordedDesktop(OfflineLinuxDesktop):
            def build_argv(self, root):
                argv = super().build_argv(root)
                index = argv.index("--")
                argv[index:index] = ["--ro-bind", str(observer), "/opt/qa_observer.py"]
                argv[-1] = "/opt/qa_observer.py"
                return argv

        desktop = RecordedDesktop("owner", workspace)
        try:
            (workspace / "painter.py").write_text(PAINTER)
            (workspace / "color").write_text("FFFFFF")
            desktop.call("owner", "launch", {"command": "python /workspace/painter.py"})
            until(lambda: (workspace / "painted").exists())
            dialog = desktop.call(
                "owner",
                "launch",
                {
                    "command": "xmessage -geometry 500x160+200+200 -buttons 'Accept:37' -default Accept "
                    + shlex.quote("Synthetic native frame-wait test")
                    + '; result=$?; printf "%s" "$result" > /workspace/dialog-result; if [ "$result" -eq 37 ]; then printf accepted > /workspace/accepted; fi'
                },
            )
            time.sleep(0.5)
            focus = desktop.call(
                "owner",
                "run",
                {
                    "command": "xdotool search --class Xmessage windowmap --sync windowactivate --sync",
                    "timeout": 3,
                },
            )
            assert focus["exit_code"] == 0, focus
            frame = desktop.call("owner", "observe", {})
            (evidence_dir / f"initial-{cancelled}.png").write_bytes(base64.b64decode(frame["png"]))
            original_hash = hashlib.sha256(base64.b64decode(frame["png"])).hexdigest()
            (workspace / "color").write_text("102040")
            until(lambda: (workspace / "painted").read_text() == "102040")
            assert desktop.capture() != base64.b64decode(frame["png"])
            (workspace / "arm-capture").touch()
            restored = False

            def cancel():
                nonlocal restored
                marker = workspace / "capture-entered"
                if not marker.exists():
                    return False
                # This marker comes after the production capture returned real
                # changed pixels, so cancellation cannot preempt the tested wait.
                assert marker.read_text() != original_hash
                if cancelled:
                    return True
                if not restored:
                    (workspace / "color").write_text("FFFFFF")
                    restored = True
                return False

            started = time.monotonic()
            try:
                result = desktop.call(
                    "owner",
                    # This controlled fixture's visible Accept button, checked
                    # in its actual PNG; this is not a model-selected action.
                    "click",
                    {"frame_id": frame["frame_id"], "x": 240, "y": 364},
                    cancel=cancel,
                )
                assert not cancelled and result["dispatched"] and restored
                (evidence_dir / "after-input.png").write_bytes(desktop.capture())
                try:
                    until(lambda: (workspace / "accepted").exists())
                except AssertionError:
                    print(
                        json.dumps(
                            {
                                "dialog": desktop.call(
                                    "owner", "status", {"process_id": dialog["process_id"]}
                                ),
                                "files": {
                                    p.name: p.read_text()
                                    for p in workspace.iterdir()
                                    if p.is_file()
                                    and p.name in {"accepted", "dialog-result", "painted"}
                                },
                                "pointer": desktop.call(
                                    "owner",
                                    "run",
                                    {
                                        "command": "xdotool getmouselocation --shell; xdotool getwindowfocus getwindowname"
                                    },
                                ),
                            }
                        ),
                        flush=True,
                    )
                    raise
                assert (workspace / "accepted").read_text() == "accepted"
            except CooperativeCancellationError:
                assert cancelled
                assert not (workspace / "accepted").exists()
                assert not desktop.alive() and desktop._process.poll() is not None
            elapsed = time.monotonic() - started
            assert elapsed < 2
            assert (workspace / "capture-entered").exists()
            if cancelled:
                time.sleep(1.3)
                assert not (workspace / "accepted").exists()
            return {
                "cancelled": cancelled,
                "actual_changed_pixels_observed": True,
                "accepted": (workspace / "accepted").exists(),
                "seconds": elapsed,
                "child_closed": not desktop.alive() if cancelled else None,
            }
        finally:
            desktop.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    evidence = parser.parse_args().evidence
    evidence.mkdir(parents=True, exist_ok=True)
    print(
        json.dumps(
            {
                "passed": True,
                "cases": [study(False, evidence), study(True, evidence)],
                "live_model_calls": 0,
            }
        )
    )
