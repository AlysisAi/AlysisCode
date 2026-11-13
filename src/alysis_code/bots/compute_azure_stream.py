"""Bounded computer messages over Azure's authenticated exec/stream socket.

The provider starts bounded, unprivileged stdio relays. No sandbox ingress,
controller credential in the guest, terminal echo, or automatic input replay.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from uuid import UUID

from .compute import ComputeUncertain

MAX_REPLY = 4 * 1024 * 1024


class AzureComputerStream:
    """Keep frame capture off the ordered, non-replayable input channel."""

    def __init__(self, screen, inputs):
        self._screen, self._inputs = screen, inputs
        self._closed = False
        self._closing = threading.Lock()

    @property
    def closed(self):
        return self._closed or self._screen.closed or self._inputs.closed

    def request(self, request, *, timeout):
        if self.closed:
            raise ComputeUncertain("Azure computer stream is disconnected.")
        channel = self._screen if request.get("operation") == "screen" else self._inputs
        try:
            return channel.request(request, timeout=timeout)
        except Exception:
            # Either uncertain channel fences the whole control connection.
            self.close()
            raise

    def close(self):
        with self._closing:
            if self._closed:
                return
            self._closed = True
        self._screen.close()
        self._inputs.close()


class AzureExecStream:
    def __init__(self, websocket, session, valid):
        self.websocket, self.session, self.valid = websocket, session, valid
        self.closed = False
        self._io = threading.Lock()
        self._closing = threading.Lock()
        self._buffer = b""

    def start(self, argv):
        try:
            self.websocket.send(
                json.dumps(
                    {
                        "type": "start",
                        "start": {
                            "command": argv[0],
                            "args": argv[1:],
                            "environment": {"LANG": "C.UTF-8"},
                            "tty": True,
                            "stdin": True,
                            "height": 24,
                            "width": 80,
                        },
                    }
                )
            )
            ready = self._line(time.monotonic() + 10)
            if (
                ready
                != {
                    "ready": True,
                    "session": self.session,
                    "uid": 1000,
                    "protocol": "computer-stream-v2",
                }
                or not self.valid()
            ):
                raise ValueError
        except Exception:
            self.close()
            raise ComputeUncertain("Azure computer stream did not become ready.") from None

    def _line(self, deadline):
        while True:
            if b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                if len(line) > MAX_REPLY:
                    raise ValueError
                return json.loads(line)
            if len(self._buffer) > MAX_REPLY or self.closed or time.monotonic() >= deadline:
                raise ValueError
            message = json.loads(
                self.websocket.recv(timeout=max(0.001, deadline - time.monotonic()))
            )
            kind = message.get("type", "").lower().replace("_", "")
            if kind == "stdout":
                data = message.get("data")
                if not isinstance(data, str) or len(data) > (MAX_REPLY + 1) * 4 // 3 + 4:
                    raise ValueError
                self._buffer += base64.b64decode(data, validate=True)
                if len(self._buffer) > MAX_REPLY + 1:
                    raise ValueError
            elif kind != "sessionid":
                # stderr/exit/error can contain private upstream details. End
                # the channel without copying them into application errors.
                raise ValueError

    def request(self, request, *, timeout):
        wire = json.dumps(request, separators=(",", ":"))
        if (
            set(request) != {"id", "session", "operation", "arguments"}
            or request["session"] != self.session
            or UUID(request["id"]).hex != request["id"]
            or request["operation"] not in {"screen", "human_input"}
            or not isinstance(request["arguments"], dict)
            or (request["operation"] == "screen" and request["arguments"])
            or len(wire.encode()) > 48000
            or not 0 < timeout <= 15
        ):
            raise ComputeUncertain("Invalid Azure computer stream request.")
        deadline = time.monotonic() + timeout
        if not self._io.acquire(timeout=timeout):
            raise ComputeUncertain("Azure computer stream is busy.")
        try:
            if self.closed or not self.valid() or time.monotonic() >= deadline:
                raise ValueError
            self.websocket.send(
                json.dumps(
                    {"type": "stdin", "data": base64.b64encode((wire + "\n").encode()).decode()}
                )
            )
            reply = self._line(deadline)
            if (
                not self.valid()
                or not isinstance(reply, dict)
                or reply.get("id") != request["id"]
                or reply.get("session") != self.session
                or not (isinstance(reply.get("result"), dict) ^ isinstance(reply.get("error"), str))
            ):
                raise ValueError
            return reply
        except Exception:
            self.close()
            raise ComputeUncertain(
                "Azure computer stream receipt was not confirmed; do not replay."
            ) from None
        finally:
            self._io.release()

    def close(self):
        with self._closing:
            if self.closed:
                return
            self.closed = True
        try:
            self.websocket.close()
        except Exception:
            pass
        self._buffer = b""
