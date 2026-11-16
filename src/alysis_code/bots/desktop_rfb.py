"""Private, read-only RFB streams. No listening socket or input authority.

x11vnc runs over an inherited socketpair on the executor's own display. Human
input still requires ComputerControls; RFB cannot inject keys or clipboard data.
Streams are bounded, ordered, never replayed, and expire when a viewer leaves.
This module also runs standalone in the offline desktop namespace.
"""

from __future__ import annotations

import base64
import re
import select
import socket
import subprocess
import threading
import time

MAX_INPUT = 32768
MAX_OUTPUT = 192 * 1024


def validate(value):
    if not isinstance(value, dict) or set(value) != {"client_id", "action", "sequence", "data"}:
        raise ValueError("Invalid desktop stream request.")
    if not isinstance(value["client_id"], str) or not re.fullmatch(
        r"[a-f0-9]{32}", value["client_id"]
    ):
        raise ValueError("Invalid desktop stream identity.")
    if value["action"] not in {"open", "exchange", "close"}:
        raise ValueError("Invalid desktop stream action.")
    if type(value["sequence"]) is not int or not 0 <= value["sequence"] <= 2**53 - 1:
        raise ValueError("Invalid desktop stream sequence.")
    data = value["data"]
    if not isinstance(data, str) or len(data) > 43692:
        raise ValueError("Desktop stream request too large.")
    raw = base64.b64decode(data, validate=True)
    if len(raw) > MAX_INPUT or (value["action"] != "exchange" and raw):
        raise ValueError("Invalid desktop stream data.")
    return raw


class RfbSessions:
    def __init__(self, display, env):
        if not re.fullmatch(r":[0-9]{1,5}", display):
            raise ValueError("A private display is required.")
        self.display, self.env = display, env
        self.sessions = {}
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        threading.Thread(target=self._reap, daemon=True).start()

    def _drop(self, identity):
        item = self.sessions.pop(identity, None)
        if item:
            connection, process, *_ = item
            connection.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)

    def _reap(self):
        while not self.stopped.wait(2):
            with self.lock:
                for identity, item in list(self.sessions.items()):
                    if time.monotonic() - item[3] > 15 or item[1].poll() is not None:
                        self._drop(identity)

    def close(self):
        self.stopped.set()
        with self.lock:
            for identity in list(self.sessions):
                self._drop(identity)

    def exchange(self, value):
        data = validate(value)
        identity, action, sequence = (value[k] for k in ("client_id", "action", "sequence"))
        with self.lock:
            if self.stopped.is_set():
                raise ValueError("Desktop stream closed.")
            if action == "close":
                self._drop(identity)
                return {"data": "", "sequence": sequence, "closed": True}
            if action == "open":
                if sequence != 0 or identity in self.sessions or len(self.sessions) >= 2:
                    raise ValueError("Desktop stream already open or busy.")
                parent, child = socket.socketpair()
                try:
                    process = subprocess.Popen(
                        [
                            "/usr/bin/x11vnc",
                            "-norc",
                            "-inetd",
                            "-display",
                            self.display,
                            # The trusted worker and shared X server have separate
                            # IPC namespaces. MIT-SHM cannot cross that boundary.
                            "-noshm",
                            "-viewonly",
                            "-nosel",
                            "-noclipboard",
                            "-no6",
                            "-nolookup",
                            "-quiet",
                            "-nopw",
                            "-wait",
                            "20",
                        ],
                        stdin=child,
                        stdout=child,
                        stderr=subprocess.DEVNULL,
                        env=self.env,
                        close_fds=True,
                    )
                except BaseException:
                    parent.close()
                    raise
                finally:
                    child.close()
                self.sessions[identity] = [parent, process, 0, time.monotonic()]
            item = self.sessions.get(identity)
            if not item:
                raise ValueError("Desktop stream expired; reconnect explicitly.")
            connection, process, previous, _ = item
            try:
                if action == "exchange" and sequence != previous + 1:
                    raise ValueError("Desktop stream sequence mismatch.")
                item[2], item[3] = sequence, time.monotonic()
                connection.settimeout(1)
                if data:
                    connection.sendall(data)
                output = bytearray()
                # Wait for changes instead of requesting/encoding full screenshots.
                if select.select([connection], [], [], 0.1)[0]:
                    while len(output) < MAX_OUTPUT:
                        chunk = connection.recv(min(65536, MAX_OUTPUT - len(output)))
                        if not chunk:
                            raise ValueError("Desktop stream disconnected.")
                        output.extend(chunk)
                        if not select.select([connection], [], [], 0)[0]:
                            break
                if process.poll() is not None:
                    raise ValueError("Desktop stream unavailable.")
                return {"sequence": sequence, "data": base64.b64encode(output).decode()}
            except BaseException:
                self._drop(identity)
                raise
