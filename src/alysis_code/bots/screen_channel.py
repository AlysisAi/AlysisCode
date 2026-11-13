"""Private read-only screen transport shared by the cloud bridge and offline child.

Standalone standard-library module: the offline jail mounts this file, never the
service source or credentials. Requests cannot select a display or invoke actions.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import threading
from types import MethodType
from uuid import UUID, uuid4


def receive(connection, *, limit=4 * 1024 * 1024):
    data = bytearray()
    while b"\n" not in data:
        part = connection.recv(65536)
        if not part or len(data) + len(part) > limit:
            raise ValueError("Incomplete or oversized screen message.")
        data.extend(part)
    line, rest = data.split(b"\n", 1)
    if rest:
        raise ValueError("Unexpected trailing screen data.")
    return json.loads(line)


def capture(directory, session, *, timeout=3):
    identity = uuid4().hex
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(timeout)
        connection.connect(str(directory / "screen.sock"))
        connection.sendall(json.dumps({"id": identity, "session": session}).encode() + b"\n")
        reply = receive(connection)
    if reply.get("id") != identity or reply.get("session") != session:
        raise ValueError("No current screen receipt.")
    pixels = base64.b64decode(reply["result"]["png"], validate=True)
    if len(pixels) > 2_000_000:
        raise ValueError("Screen exceeds allowance.")
    return pixels


class ScreenServer:
    """Private read-only channel, independent of serialized agent operations.

    Only trusted worker code supplies a capture callback. Requests cannot select
    a display or execute an operation. Revocation fences in-flight captures.
    """

    def __init__(self, directory, session):
        self.session = session
        self.capture = None
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(directory / "screen.sock"))
        os.chmod(directory / "screen.sock", 0o600)
        self.listener.listen(1)
        self.listener.settimeout(0.2)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def bind(self, capture):
        with self.lock:
            current = self.capture
            if capture is current or (
                isinstance(capture, MethodType)
                and isinstance(current, MethodType)
                and capture.__self__ is current.__self__
                and capture.__func__ is current.__func__
            ):
                # Re-accessing an executor method creates a new bound-method
                # object. Input on that same display must not revoke its frame.
                return
            self.capture = capture

    def _serve(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(4)
                try:
                    request = receive(connection, limit=512)
                    identity = request.get("id")
                    if (
                        set(request) != {"id", "session"}
                        or request["session"] != self.session
                        or not isinstance(identity, str)
                        or UUID(identity).hex != identity
                    ):
                        raise ValueError("Invalid screen request.")
                    with self.lock:
                        capture = self.capture
                    if capture is None:
                        raise ValueError("No screen bound.")
                    pixels = capture()
                    with self.lock:
                        if capture is not self.capture or self.stop.is_set():
                            raise ValueError("Screen revoked.")
                    if len(pixels) > 2_000_000:
                        raise ValueError("Screen exceeds allowance.")
                    reply = {
                        "id": identity,
                        "session": self.session,
                        "result": {"png": base64.b64encode(pixels).decode()},
                    }
                    connection.sendall(json.dumps(reply).encode() + b"\n")
                except Exception:
                    # Viewing never interrupts work or reports an old frame.
                    try:
                        connection.sendall(b'{"error":"Screen unavailable."}\n')
                    except OSError:
                        pass

    def close(self):
        self.bind(None)
        self.stop.set()
        self.listener.close()
        self.thread.join(timeout=5)
