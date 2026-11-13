"""Provider-free real Linux qualification of the offline desktop child.

Run in the reserved disposable lab only. No model, external accounts or network.
Synthetic credential sentinels are not actual credentials.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

from alysis_code.bots.desktop_linux import DesktopError, OfflineLinuxDesktop
from alysis_code.cancellation import CooperativeCancellationError

FORM_SERVER = r'''
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import json
PAGE = b"""<!doctype html><meta charset=utf-8><style>
body{font:24px sans-serif;margin:40px;min-height:2500px}input,button{font:24px sans-serif;padding:16px}
</style><h1>Offline desktop input study</h1><label>Note <input id=note autofocus></label>
<button id=save>Save</button><p id=result>Ready</p><script>
save.onclick=async()=>{await fetch('/save',{method:'POST',body:JSON.stringify({text:note.value})});result.textContent='Saved: '+note.value};
window.onscroll=()=>fetch('/scroll',{method:'POST',body:JSON.stringify({scroll_y:scrollY})});
</script>"""
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(PAGE)
        Path('/workspace/ready').touch()
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        name='scroll-receipt.json' if self.path=='/scroll' else 'form-receipt.json'
        Path('/workspace/'+name).write_text(json.dumps(data,ensure_ascii=False))
        if self.path=='/save':
            with Path('/workspace/form-events.jsonl').open('a') as log: log.write(json.dumps(data,ensure_ascii=False)+'\n')
        self.send_response(200);self.end_headers()
    def log_message(self,*_): pass
HTTPServer(('127.0.0.1',8766),Handler).serve_forever()
'''


def main(evidence_dir=None):
    checks = []
    with tempfile.TemporaryDirectory(prefix="alysis-desktop-boundary-study-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        sentinel = "synthetic-private-" + os.urandom(8).hex()
        private = root / "service-private"
        private.write_text(sentinel)
        os.environ["ALYSIS_BOUNDARY_SENTINEL"] = sentinel
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with socket.create_connection(listener.getsockname(), timeout=1):
            connection, _ = listener.accept()
            connection.close()
        computer = OfflineLinuxDesktop("study-owner", workspace)
        try:
            pointer = computer.call(
                "study-owner", "run", {"command": "xdotool getmouselocation --shell"}
            )
            coordinates = dict(line.split("=", 1) for line in pointer["output"].splitlines())
            for operation, extra in (("scroll", {"ticks": 2}), ("click", {})):
                frame = computer.call("study-owner", "observe", {})
                started = time.monotonic()
                result = computer.call(
                    "study-owner",
                    operation,
                    {
                        "frame_id": frame["frame_id"],
                        "x": int(coordinates["X"]),
                        "y": int(coordinates["Y"]),
                        **extra,
                    },
                )
                assert result["dispatched"] and time.monotonic() - started < 2
                assert computer.alive()
            checks.append(
                "Scrolling and clicking at the actual unchanged pointer position do not hang"
            )
            boundary_code = """import os, pathlib, socket, json
report = {"environment_hidden": "ALYSIS_BOUNDARY_SENTINEL" not in os.environ,
          "service_hidden": not pathlib.Path("/study-src").exists(),
          "parent_private_hidden": not pathlib.Path(PRIVATE).exists(),
          "sockets": []}
for address, port in (("127.0.0.1", PORT), ("1.1.1.1", 80), ("169.254.169.254", 80)):
    try:
        with socket.create_connection((address, port), timeout=0.1): report["sockets"].append(True)
    except OSError: report["sockets"].append(False)
processes = []
for entry in pathlib.Path("/proc").iterdir():
    if entry.name.isdigit():
        try: processes.append((entry / "cmdline").read_bytes().decode(errors="replace"))
        except OSError: pass
report["controller_hidden"] = not any("/opt/desktop_worker.py" in p and "-I" in p for p in processes)
print(json.dumps(report))
""".replace("PRIVATE", repr(str(private))).replace("PORT", str(listener.getsockname()[1]))
            code_path = workspace / "boundary.py"
            code_path.write_text(boundary_code)
            receipt = computer.call(
                "study-owner", "run", {"command": "python /workspace/boundary.py"}
            )
            assert receipt["exit_code"] == 0, receipt
            evidence = json.loads(receipt["output"])
            assert all(
                evidence[k]
                for k in (
                    "environment_hidden",
                    "service_hidden",
                    "parent_private_hidden",
                    "controller_hidden",
                )
            ), evidence
            assert evidence["sockets"] == [False, False, False], evidence
            checks.append(
                "Private environment, service paths, controller processes and network are inaccessible"
            )

            receipt = computer.call(
                "study-owner", "run", {"command": "printf '日本語 — Ω' > note.txt; cat note.txt"}
            )
            assert (
                receipt["output"] == "日本語 — Ω"
                and (workspace / "note.txt").read_text() == receipt["output"]
            )
            checks.append("Command creates an actual UTF-8 workspace artifact")
            receipt = computer.call(
                "study-owner", "run", {"command": "sleep 10; touch too-late", "timeout": 1}
            )
            assert receipt["timed_out"] and not (workspace / "too-late").exists(), receipt
            receipt = computer.call("study-owner", "run", {"command": "yes output"})
            assert receipt["output_truncated"] and len(receipt["output"]) == 64000, receipt
            checks.append("Timeout and output overflow terminate actual command namespaces")

            job = computer.call(
                "study-owner",
                "launch",
                {
                    "command": "xmessage -geometry 500x160+200+200 -buttons 'Save:0,Cancel:1' -default Save 'Desktop boundary study'; printf '%s' $? > selection.txt"
                },
            )
            time.sleep(0.5)
            image = computer.call("study-owner", "observe", {})
            assert image["width"] == 1280 and image["height"] == 800
            (workspace / "desktop.png").write_bytes(base64.b64decode(image["png"]))
            if evidence_dir:
                (evidence_dir / "native-dialog.png").write_bytes(base64.b64decode(image["png"]))
            result = computer.call(
                "study-owner", "key", {"frame_id": image["frame_id"], "keys": ["Return"]}
            )
            assert result["dispatched"]
            deadline = time.monotonic() + 3
            while not (workspace / "selection.txt").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert (workspace / "selection.txt").read_text() == "0"
            assert (
                computer.call("study-owner", "status", {"process_id": job["process_id"]})[
                    "exit_code"
                ]
                == 0
            )
            checks.append(
                "Current-frame actual X11 key activates a native dialog and creates a real file receipt"
            )
            try:
                computer.call(
                    "study-owner", "key", {"frame_id": image["frame_id"], "keys": ["Return"]}
                )
            except DesktopError as exc:
                assert "expired" in str(exc)
            else:
                raise AssertionError("Used frame replayed")
            try:
                computer.call("other-owner", "observe", {})
            except DesktopError:
                pass
            else:
                raise AssertionError("Other owner used computer")
            checks.append("Wrong-owner and consumed-frame input rejected")

            (workspace / "server.py").write_text(FORM_SERVER)
            computer.call("study-owner", "launch", {"command": "python /workspace/server.py"})
            browser_job = computer.call(
                "study-owner",
                "launch",
                {
                    "command": "chromium --disable-dev-shm-usage --no-first-run --no-default-browser-check --user-data-dir=/tmp/chrome --kiosk http://127.0.0.1:8766/"
                },
            )
            deadline = time.monotonic() + 10
            while not (workspace / "ready").exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            assert (workspace / "ready").exists(), computer.call(
                "study-owner", "status", {"process_id": browser_job["process_id"]}
            )
            time.sleep(0.3)
            text = "日本語 — Ω — desktop"
            previous = None
            for operation, arguments in (
                ("type", {"text": text}),
                ("key", {"keys": ["Tab"]}),
                ("key", {"keys": ["Return"]}),
            ):
                frame = previous or computer.call("study-owner", "observe", {})
                if operation == "key" and arguments["keys"] == ["Tab"]:
                    # Positive control: the caret really changed the observed
                    # image before dispatch. The action must await exact pixels.
                    deadline = time.monotonic() + 2
                    while computer.capture() == base64.b64decode(frame["png"]):
                        assert time.monotonic() < deadline, "No real caret phase change observed"
                        time.sleep(0.05)
                result = computer.call(
                    "study-owner", operation, {"frame_id": frame["frame_id"], **arguments}
                )
                previous = result["observation"]
            deadline = time.monotonic() + 3
            while not (workspace / "form-receipt.json").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            if evidence_dir:
                (evidence_dir / "desktop-form.png").write_bytes(computer.capture())
                (evidence_dir / "form-receipt.json").write_text(
                    (workspace / "form-receipt.json").read_text()
                )
            actual_text = json.loads((workspace / "form-receipt.json").read_text())["text"]
            assert actual_text == text, repr(actual_text)
            checks.append(
                "Real Unicode paste, changed caret phase, Tab and Enter save an independently verified form"
            )
            frame = computer.call("study-owner", "observe", {})
            computer.call(
                "study-owner", "click", {"frame_id": frame["frame_id"], "x": 535, "y": 175}
            )
            time.sleep(0.15)
            receipts = [
                json.loads(line)
                for line in (workspace / "form-events.jsonl").read_text().splitlines()
            ]
            assert len(receipts) == 2 and all(row["text"] == text for row in receipts), receipts
            frame = computer.call("study-owner", "observe", {})
            computer.call(
                "study-owner",
                "scroll",
                {"frame_id": frame["frame_id"], "x": 1000, "y": 650, "ticks": 6},
            )
            deadline = time.monotonic() + 3
            while not (workspace / "scroll-receipt.json").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert json.loads((workspace / "scroll-receipt.json").read_text())["scroll_y"] > 0
            checks.append(
                "Actual X11 click produces one more receipt and wheel input scrolls the page"
            )
            (workspace / "wm-check.py").write_text("""import json, os
from pathlib import Path
result = {"controller_visible":False,"controller_stdout_openable":False}
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit(): continue
    try:
        if b'/opt/desktop_worker.py' not in (entry/'cmdline').read_bytes().split(b'\\0'): continue
        result['controller_visible']=True
        fd=os.open(str(entry/'fd/1'),os.O_WRONLY|os.O_NONBLOCK);os.close(fd)
        result['controller_stdout_openable']=True
    except OSError: pass
Path('/workspace/wm-receipt.json').write_text(json.dumps(result))
""")
            (workspace / "rc.xml").write_text("""<openbox_config xmlns="http://openbox.org/3.4/rc">
<keyboard><keybind key="F12"><action name="Execute"><command>python /workspace/wm-check.py</command></action></keybind></keyboard>
</openbox_config>""")
            receipt = computer.call(
                "study-owner",
                "run",
                {
                    "command": "mkdir -p /tmp/home/.config/openbox; cp /workspace/rc.xml /tmp/home/.config/openbox/rc.xml; openbox --reconfigure; sleep 1; xdotool key F12; sleep 1"
                },
            )
            assert receipt["exit_code"] == 0, receipt
            assert (workspace / "wm-receipt.json").exists(), (
                "Window-manager launch must actually run for this check"
            )
            wm_receipt = json.loads((workspace / "wm-receipt.json").read_text())
            assert wm_receipt == {
                "controller_visible": False,
                "controller_stdout_openable": False,
            }, wm_receipt
            if evidence_dir:
                (evidence_dir / "wm-receipt.json").write_text(json.dumps(wm_receipt))
            checks.append(
                "A real Openbox-config Execute action cannot discover or open the controller's reply pipe"
            )

            # The underlying computer and every application must die on Stop,
            # including a command that would otherwise write after cancellation.
            cancel = threading.Event()
            timer = threading.Timer(0.3, cancel.set)
            timer.start()
            try:
                computer.call(
                    "study-owner",
                    "run",
                    {"command": "sleep 5; touch after-stop", "timeout": 10},
                    cancel=cancel.is_set,
                )
            except CooperativeCancellationError:
                pass
            else:
                raise AssertionError("Cancellation was ignored")
            timer.join()
            assert not computer.alive()
            time.sleep(5.1)
            assert not (workspace / "after-stop").exists()
            checks.append(
                "Stop closes the entire owned child instead of abandoning an executing command"
            )
        finally:
            computer.close()
            listener.close()
            os.environ.pop("ALYSIS_BOUNDARY_SENTINEL", None)
        print(json.dumps({"passed": True, "checks": checks, "model_calls": 0}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.evidence:
        args.evidence.mkdir(parents=True, exist_ok=True)
    main(args.evidence)
