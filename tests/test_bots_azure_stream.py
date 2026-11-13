"""Azure exec/stream wire contract checks. Live Azure evidence is separate."""

import base64
import io
import json
from uuid import uuid4

import pytest

from alysis_code.bots.compute import ComputeUncertain
from alysis_code.bots.compute_azure_stream import AzureExecStream


class Socket:
    def __init__(self):
        self.sent, self.incoming = [], []
        self.closed = False

    def send(self, wire):
        self.sent.append(json.loads(wire))

    def recv(self, *, timeout):
        assert timeout > 0
        if not self.incoming:
            raise TimeoutError
        return json.dumps(self.incoming.pop(0))

    def stdout(self, data):
        self.incoming.append({"type": "stdout", "data": base64.b64encode(data).decode()})

    def close(self):
        self.closed = True


def setup():
    socket, session = Socket(), uuid4().hex
    ready = (
        json.dumps(
            {"ready": True, "session": session, "uid": 1000, "protocol": "computer-stream-v2"}
        ).encode()
        + b"\n"
    )
    socket.incoming.append({"type": "sessionId", "data": "opaque-provider-session"})
    socket.stdout(ready[:19])
    socket.stdout(ready[19:])
    owned = [True]
    link = AzureExecStream(socket, session, lambda: owned[0])
    link.start(["/trusted/python", "-I", "relay"])
    return socket, link, owned


def request(link):
    return {"id": uuid4().hex, "session": link.session, "operation": "screen", "arguments": {}}


def test_terminal_stream_accepts_fragmented_stdout_and_exact_receipts():
    socket, link, _ = setup()
    try:
        assert socket.sent[0]["start"]["tty"] is True
        for _ in range(3):
            message = request(link)
            reply = {"id": message["id"], "session": link.session, "result": {"png": "cGl4ZWxz"}}
            wire = json.dumps(reply).encode() + b"\n"
            socket.stdout(wire[:31])
            socket.stdout(wire[31:])
            assert link.request(message, timeout=1) == reply
            assert json.loads(base64.b64decode(socket.sent[-1]["data"])) == message
    finally:
        link.close()
    assert socket.closed


@pytest.mark.parametrize(
    "failure",
    ["timeout", "foreign_session", "foreign_id", "stderr", "exit", "invalid_base64", "ownership"],
)
def test_lost_or_foreign_receipt_closes_without_replaying_input(failure):
    socket, link, owned = setup()
    message = {
        **request(link),
        "operation": "human_input",
        "arguments": {"ciphertext": "sealed-input"},
    }
    if failure in {"foreign_session", "foreign_id"}:
        reply = {"id": message["id"], "session": link.session, "result": {"applied": True}}
        reply["session" if failure == "foreign_session" else "id"] = uuid4().hex
        socket.stdout(json.dumps(reply).encode() + b"\n")
    elif failure == "stderr":
        socket.incoming.append({"type": "stderr", "data": "private upstream content"})
    elif failure == "exit":
        socket.incoming.append({"type": "exitCode", "exitCode": 1})
    elif failure == "invalid_base64":
        socket.incoming.append({"type": "stdout", "data": "!!"})
    elif failure == "ownership":
        owned[0] = False
    with pytest.raises(ComputeUncertain) as error:
        link.request(message, timeout=0.1)
    assert "private" not in str(error.value)
    count = len(socket.sent)
    assert socket.closed and link.closed
    with pytest.raises(ComputeUncertain):
        link.request(message, timeout=0.1)
    assert len(socket.sent) == count


def test_guest_stdio_forwards_only_screen_and_sequence_encrypted_input(monkeypatch):
    from alysis_code.bots import computer_stream_worker as worker

    session = uuid4().hex
    screen = {"id": uuid4().hex, "session": session, "operation": "screen", "arguments": {}}
    gesture = {
        "id": uuid4().hex,
        "session": session,
        "operation": "human_input",
        "arguments": {"ciphertext": "sealed"},
    }
    incoming = io.TextIOWrapper(
        io.BytesIO(b"".join(json.dumps(r).encode() + b"\n" for r in (screen, gesture)))
    )
    output, calls = io.StringIO(), []
    from PIL import Image

    pixels = io.BytesIO()
    Image.new("RGB", (80, 40), "blue").save(pixels, format="PNG")

    def exchange(current, message, **kw):
        calls.append((current, message, kw))
        return {
            "id": message["id"],
            "session": current,
            "result": {"png": base64.b64encode(pixels.getvalue()).decode()}
            if kw["screen"]
            else {"confirmed": True},
        }

    monkeypatch.setattr(worker.sys, "stdin", incoming)
    monkeypatch.setattr(worker.sys, "stdout", output)
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(worker, "exchange", exchange)
    worker.stdio(session)
    assert [json.loads(line) for line in output.getvalue().splitlines()][0]["uid"] == 1000
    assert calls[0][1] == {"id": screen["id"], "session": session} and calls[0][2]["screen"] is True
    assert calls[1][1] == gesture and calls[1][2]["screen"] is False


@pytest.mark.parametrize(
    "change", [{"operation": "execute"}, {"session": "foreign"}, {"arguments": {"untrusted": True}}]
)
def test_guest_stdio_rejects_commands_outside_the_computer_protocol(monkeypatch, change):
    from alysis_code.bots import computer_stream_worker as worker

    session = uuid4().hex
    message = {
        "id": uuid4().hex,
        "session": session,
        "operation": "screen",
        "arguments": {},
        **change,
    }
    monkeypatch.setattr(
        worker.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(message).encode() + b"\n"))
    )
    monkeypatch.setattr(
        worker, "exchange", lambda *a, **k: pytest.fail("Invalid request reached guest")
    )
    with pytest.raises(ValueError):
        worker.stdio(session)


def test_provider_uses_its_scoped_wss_endpoint_without_a_token_in_the_url(monkeypatch):
    import websockets.sync.client
    from test_bots_compute import AzureWire

    wire = AzureWire()
    calls, sockets = [], []

    def connect(url, **kwargs):
        calls.append((url, kwargs))
        socket = Socket()
        socket.stdout(
            json.dumps(
                {
                    "ready": True,
                    "session": wire.identity.allocation_id,
                    "uid": 1000,
                    "protocol": "computer-stream-v2",
                }
            ).encode()
            + b"\n"
        )
        sockets.append(socket)
        return socket

    monkeypatch.setattr(websockets.sync.client, "connect", connect)
    try:
        link = wire.provider.open_stream(
            wire.ref, ["/trusted/python", "-I", "relay"], valid=lambda: True
        )
        url, options = calls[0]
        assert url.startswith("wss://management.swedencentral.azuredevcompute.io/")
        assert url.endswith("/" + wire.id + "/exec/stream?api-version=2026-02-01-preview")
        assert "synthetic-test-token" not in url
        assert options["additional_headers"] == {"Authorization": "Bearer synthetic-test-token"}
        assert options["proxy"] is None
        assert len(calls) == 2 and calls[0] == calls[1]
        link.close()
        assert all(socket.closed for socket in sockets)
        wire.provider.expires_at = 1
        with pytest.raises(Exception, match="owned computer"):
            wire.provider.open_stream(wire.ref, ["/trusted/python"], valid=lambda: True)
        assert len(calls) == 2
    finally:
        wire.provider.close()


def test_slow_native_capture_does_not_queue_in_front_of_human_input():
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from alysis_code.bots.compute_azure_stream import AzureComputerStream

    entered, release = threading.Event(), threading.Event()

    class Channel:
        closed = False

        def request(self, message, *, timeout):
            if message["operation"] == "screen":
                entered.set()
                assert release.wait(2)
            return {"applied": message["operation"]}

        def close(self):
            self.closed = True

    screen, inputs = Channel(), Channel()
    link = AzureComputerStream(screen, inputs)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(link.request, {"operation": "screen"}, timeout=1)
        try:
            assert entered.wait(1)
            gesture = pool.submit(link.request, {"operation": "human_input"}, timeout=1)
            assert gesture.result(1) == {"applied": "human_input"}
            assert not pending.done()
        finally:
            release.set()
            pending.result(2)
            link.close()
    assert screen.closed and inputs.closed


def test_partial_native_connection_setup_closes_the_first_channel(monkeypatch):
    import websockets.sync.client
    from test_bots_compute import AzureWire

    wire = AzureWire()
    socket = Socket()
    socket.stdout(
        json.dumps(
            {
                "ready": True,
                "session": wire.identity.allocation_id,
                "uid": 1000,
                "protocol": "computer-stream-v2",
            }
        ).encode()
        + b"\n"
    )
    calls = []

    def connect(url, **kwargs):
        calls.append(url)
        if len(calls) == 2:
            raise OSError("private upstream details")
        return socket

    monkeypatch.setattr(websockets.sync.client, "connect", connect)
    try:
        with pytest.raises(ComputeUncertain, match="could not be established") as error:
            wire.provider.open_stream(wire.ref, ["/trusted/python"], valid=lambda: True)
        assert "private upstream" not in str(error.value)
        assert len(calls) == 2 and socket.closed
    finally:
        wire.provider.close()


@pytest.mark.parametrize("operation", ["screen", "human_input"])
def test_uncertain_native_channel_closes_both_channels_without_replay(operation):
    from alysis_code.bots.compute_azure_stream import AzureComputerStream

    class Channel:
        closed = False
        calls = 0

        def request(self, message, *, timeout):
            self.calls += 1
            raise ComputeUncertain("Receipt missing; do not replay")

        def close(self):
            self.closed = True

    screen, inputs = Channel(), Channel()
    link = AzureComputerStream(screen, inputs)
    with pytest.raises(ComputeUncertain, match="Receipt missing"):
        link.request({"operation": operation}, timeout=1)
    assert link.closed and screen.closed and inputs.closed
    with pytest.raises(ComputeUncertain, match="disconnected"):
        link.request({"operation": operation}, timeout=1)
    assert screen.calls + inputs.calls == 1


def test_guest_terminal_disables_echo_and_line_limits_before_accepting_large_encrypted_messages(
    tmp_path,
):
    import os
    import pty
    import select
    import subprocess
    import sys
    import termios

    master, slave = pty.openpty()
    session = uuid4().hex
    program = (
        "from alysis_code.bots import computer_stream_worker as w;"
        "w.exchange=lambda session,message,**kw:{'id':message['id'],'session':session,'result':{'bytes':len(message['arguments']['ciphertext'])}};"
        f"w.stdio({session!r})"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", program], stdin=slave, stdout=slave, stderr=subprocess.PIPE
    )

    def line():
        data = b""
        while not data.endswith(b"\n"):
            assert select.select([master], [], [], 3)[0]
            data += os.read(master, 65536)
        return json.loads(data)

    try:
        assert line()["protocol"] == "computer-stream-v2"
        flags = termios.tcgetattr(slave)
        assert not flags[3] & (termios.ECHO | termios.ICANON)
        assert not flags[1] & termios.OPOST
        message = {
            "id": uuid4().hex,
            "session": session,
            "operation": "human_input",
            "arguments": {"ciphertext": "sealed" * 4000},
        }
        wire = json.dumps(message).encode() + b"\n"
        # Exceeds the usual terminal canonical-line limit. An echoed request
        # would be returned instead of the exact guest receipt below.
        offset = 0
        while offset < len(wire):
            offset += os.write(master, wire[offset:])
        assert line() == {"id": message["id"], "session": session, "result": {"bytes": 24000}}
    finally:
        process.terminate()
        process.wait(timeout=3)
        os.close(master)
        os.close(slave)


def test_stream_images_preserve_full_guest_coordinates_and_drop_metadata():
    from PIL import Image, PngImagePlugin

    from alysis_code.bots.computer_stream_worker import jpeg_screen

    original = Image.new("RGB", (1600, 1000), "white")
    original.paste("blue", (1200, 700, 1500, 900))
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("comment", "private screenshot metadata")
    pixels = io.BytesIO()
    original.save(pixels, format="PNG", pnginfo=metadata)
    result = jpeg_screen({"png": base64.b64encode(pixels.getvalue()).decode()})
    assert set(result) == {"mime", "image"} and result["mime"] == "image/jpeg"
    with Image.open(io.BytesIO(base64.b64decode(result["image"], validate=True))) as image:
        assert image.size == (1600, 1000) and image.format == "JPEG"
        assert "comment" not in image.info
        blue = image.getpixel((1300, 800))
        assert blue[2] > 245 and max(blue[:2]) < 10
